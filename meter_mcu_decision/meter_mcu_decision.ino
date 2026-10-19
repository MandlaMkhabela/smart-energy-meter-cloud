/*
  26G55 Smart Energy Meter - Raspberry Pi Pico 2 W
  --------------------------------------------------------------------
  UNIFIED FIRMWARE - the SAME file runs on both MCUs.

  Role (read at boot):
    GP14 jumpered to GND  => UTILITY SIDE  (measures I1, answers PLC requests,
                                            Wi-Fi OFF)
    GP14 left open        => CUSTOMER SIDE (measures I2, requests I1 over PLC,
                                            decides bypass, publishes MQTT)
    (Fit a 2-pin header between GP14 and GND on each board.)

  Wiring (matches the upstream-node schematic):
    ATM90E26  SDO -> GP0 (MISO)   CS -> GP1   SCLK -> GP2   SDI -> GP3 (MOSI)
    ATM90E26  USEL / MMD0 / MMD1 / RESET: tie as the datasheet requires
              (SPI mode, normal metering mode, reset released). Do not leave
              them floating.
    PLC_TX_DATA  = GP4  (gate of the OOK NMOS, active-low OOK)
    MOD_CARRIER  = GP5  (150 kHz PWM carrier)
    PLC_RX       = GP6  (software UART receive from Q4)

  PLC link: request / reply packets, 2400 baud, CRC-8, sequence IDs so stale
  replies are rejected. A timeout means "link lost", NOT theft.

  Wi-Fi (customer side only): tries the networks in WIFI_NETWORKS in order,
  stays on the one that connects, and falls back to the next one if it drops
  or fails. The Pico 2 W cannot join WPA2-Enterprise networks (e.g. "Wits"),
  only open / WPA / WPA2-personal networks such as a phone hotspot.

  SAFETY: the ATM90E26 front end is not isolated from mains. Do not connect
  USB to a laptop while it is on mains unless the USB side is isolated.
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <SPI.h>
#include <SoftwareSerial.h>
#include <math.h>

// -----------------------------------------------------------------------------
// PINS
// -----------------------------------------------------------------------------
#define ROLE_PIN        14
#define ATM_MISO_PIN    0     // ATM90E26 SDO
#define ATM_CS_PIN      1
#define ATM_SCK_PIN     2
#define ATM_MOSI_PIN    3     // ATM90E26 SDI
#define PLC_TX_PIN      4
#define MOD_CARRIER_PIN 5
#define PLC_RX_PIN      6
#define PLC_BAUD        2400
#define PLC_CARRIER_HZ  150000
#define PLC_PWM_RANGE   255

// GP6 is not a hardware UART RX pin, so the PLC packet UART uses the PIO-based
// SoftwareSerial on GP6 (RX) and GP4 (TX). GP5 outputs the continuous 150 kHz
// carrier; the external NMOS performs active-low OOK switching.
SoftwareSerial plcSerial(PLC_RX_PIN, PLC_TX_PIN); // RX, TX

// -----------------------------------------------------------------------------
// PLC PACKET (packed so the CRC covers exactly the bytes that are sent)
// -----------------------------------------------------------------------------
#define PLC_HEADER        0xAA
#define PLC_TYPE_REQUEST  0
#define PLC_TYPE_REPLY    1

struct __attribute__((packed)) PLCPacket {
  uint8_t  header;       // 0xAA
  uint8_t  type;         // 0 = request, 1 = reply
  uint16_t seq;          // request sequence ID, echoed in the reply
  uint32_t timestamp;    // sender millis()
  uint16_t current_mA;   // utility RMS current
  uint16_t voltage_dV;   // utility RMS voltage, 0.1 V
  uint16_t power_W;      // utility active power
  uint16_t pf_milli;     // utility power factor x1000
  uint8_t  crc;          // CRC-8 of all previous bytes
};

// -----------------------------------------------------------------------------
// WI-FI + MQTT (customer side only)
// -----------------------------------------------------------------------------
struct WiFiNetwork {
  const char* ssid;
  const char* password;
};

// Tried in this order. Must be open or WPA/WPA2-personal (no WPA2-Enterprise).
const WiFiNetwork WIFI_NETWORKS[] = {
  {"MANDLA", "12345678"},
  {"MANDLA 9274", "1569{O3q"}
};

const size_t WIFI_NETWORK_COUNT = sizeof(WIFI_NETWORKS) / sizeof(WIFI_NETWORKS[0]);
size_t wifiNetworkIndex = 0;
uint8_t wifiFailCount = 0;     // failed attempts since the last good connection

const char* MQTT_BROKER = "broker.hivemq.com";
const uint16_t MQTT_PORT = 1883;
const char* MQTT_TOPIC = "wits/group55/mandla-main001/data";
const char* METER_ID = "main001";

// -----------------------------------------------------------------------------
// TIMING + DETECTION SETTINGS
// -----------------------------------------------------------------------------
const unsigned long METER_PERIOD_MS       = 500;    // ATM90E26 read rate
const unsigned long PUBLISH_PERIOD_MS     = 3000;   // MQTT + bypass decision
const unsigned long WIFI_RETRY_PERIOD_MS  = 15000;  // min gap between Wi-Fi attempts
const unsigned long WIFI_SETUP_WAIT_MS    = 10000;  // per-network wait at boot
const unsigned long MQTT_RETRY_PERIOD_MS  = 5000;
const unsigned long METER_RETRY_MS        = 10000;
const unsigned long PLC_REQUEST_PERIOD_MS = 1000;   // customer asks for I1
const unsigned long PLC_LINK_TIMEOUT_MS   = 5000;   // no valid reply -> link lost

const float BYPASS_THRESHOLD_A = 0.25f;  // tau - must be set by calibration
const uint8_t ALARM_ON_COUNT = 3;
const uint8_t ALARM_CLEAR_COUNT = 2;

// -----------------------------------------------------------------------------
// ATM90E26 SETTINGS
// -----------------------------------------------------------------------------
const uint32_t ATM_SPI_HZ = 200000;      // datasheet maximum

// Current PGA code -> MMode[15:13]: 0=x1, 1=x4, 2=x8, 3=x16, 4=x24 (check the
// datasheet MMode table and the input full-scale range for each gain). Must
// match the 5 mOhm shunt and your maximum load current.
const uint8_t CURRENT_PGA_CODE = 4;

// PLACEHOLDER gains from the Microchip reference design. Calibrate them for
// your own divider and shunt, or use the software trims below.
const uint16_t ATM_UGAIN  = 0xD464;
const uint16_t ATM_IGAINL = 0x6E49;
const uint16_t ATM_LGAIN  = 0x1D39;
const uint16_t ATM_PLCONST_H = 0x00B9;
const uint16_t ATM_PLCONST_L = 0xC1F3;

const float VOLTAGE_TRIM = 1.0f;
const float CURRENT_TRIM = 1.0f;
const float POWER_TRIM   = 1.0f;

// Register map (subset)
const uint8_t REG_SOFTRESET = 0x00;
const uint8_t REG_SYSSTATUS = 0x01;
const uint8_t REG_CALSTART  = 0x10;
const uint8_t REG_CAL_FIRST = 0x11;   // 0x11..0x1B
const uint8_t REG_MMODE     = 0x1B;
const uint8_t REG_CS1       = 0x1C;
const uint8_t REG_ADJSTART  = 0x20;
const uint8_t REG_ADJ_FIRST = 0x21;   // 0x21..0x2A
const uint8_t REG_CS2       = 0x2B;
const uint8_t REG_IRMS      = 0x40;
const uint8_t REG_URMS      = 0x41;
const uint8_t REG_PMEAN     = 0x42;
const uint8_t REG_FREQ      = 0x44;
const uint8_t REG_POWERF    = 0x45;

// -----------------------------------------------------------------------------
// GLOBAL STATE
// -----------------------------------------------------------------------------
bool isUtilitySide = false;

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

unsigned long lastMeterMs = 0;
unsigned long lastPublishMs = 0;
unsigned long lastWiFiAttemptMs = 0;
unsigned long lastMqttAttemptMs = 0;
unsigned long lastMeterInitMs = 0;
unsigned long sampleCount = 0;
unsigned long bootNonce = 0;

bool meterOk = false;
uint16_t meterMModeExpected = 0;

// Latest local ATM90E26 reading (whichever side this board is)
float meterVoltage = 0.0f;
float meterCurrent = 0.0f;
float meterPower = 0.0f;
float meterPF = 1.0f;
float meterFreq = 0.0f;

// Dashboard quantities
float upstreamCurrent = 0.0f;   // I1 (utility)
float upstreamPower = 0.0f;
float mainCurrent = 0.0f;       // I2 (customer)
float mainPower = 0.0f;
float differenceCurrent = 0.0f;
float differencePower = 0.0f;

float meteredEnergyWh = 0.0f;
float candidateBypassEnergyWh = 0.0f;
float possibleBypassEnergyWh = 0.0f;

// PLC state / statistics
uint16_t plcSequence = 0;
unsigned long lastPlcRequestMs = 0;
unsigned long lastPlcOkMs = 0;
bool plcLinkOk = false;
unsigned long plcTxCount = 0;
unsigned long plcRxOk = 0;
unsigned long plcRxCrcErr = 0;
unsigned long plcRxStale = 0;

uint8_t anomalyCount = 0;
uint8_t clearCount = 0;
bool bypassAlarmActive = false;


// -----------------------------------------------------------------------------
// ATM90E26 SPI DRIVER
// -----------------------------------------------------------------------------
uint16_t atmRead(uint8_t reg) {
  SPI.beginTransaction(SPISettings(ATM_SPI_HZ, MSBFIRST, SPI_MODE3));
  digitalWrite(ATM_CS_PIN, LOW);
  delayMicroseconds(10);
  SPI.transfer(reg | 0x80);               // bit 7 = 1 -> read
  delayMicroseconds(4);
  uint8_t hi = SPI.transfer(0xFF);
  uint8_t lo = SPI.transfer(0xFF);
  delayMicroseconds(10);
  digitalWrite(ATM_CS_PIN, HIGH);
  SPI.endTransaction();
  delayMicroseconds(10);
  return ((uint16_t)hi << 8) | lo;
}

void atmWrite(uint8_t reg, uint16_t value) {
  SPI.beginTransaction(SPISettings(ATM_SPI_HZ, MSBFIRST, SPI_MODE3));
  digitalWrite(ATM_CS_PIN, LOW);
  delayMicroseconds(10);
  SPI.transfer(reg & 0x7F);            // bit 7 = 0 -> write
  delayMicroseconds(4);
  SPI.transfer((uint8_t)(value >> 8));
  SPI.transfer((uint8_t)(value & 0xFF));
  delayMicroseconds(10);
  digitalWrite(ATM_CS_PIN, HIGH);
  SPI.endTransaction();
  delayMicroseconds(10);
}

// CS1/CS2: low byte = sum of all bytes mod 256, high byte = XOR of all bytes.
uint16_t atmChecksum(const uint16_t* values, size_t count) {
  uint8_t sum = 0;
  uint8_t x = 0;
  for (size_t i = 0; i < count; i++) {
    uint8_t hi = (uint8_t)(values[i] >> 8);
    uint8_t lo = (uint8_t)(values[i] & 0xFF);
    sum += hi;
    sum += lo;
    x ^= hi;
    x ^= lo;
  }
  return ((uint16_t)x << 8) | sum;
}

bool initMeteringIC() {
  Serial.println("Configuring ATM90E26...");

  atmWrite(REG_SOFTRESET, 0x789A);
  delay(500);

  meterMModeExpected = ((uint16_t)CURRENT_PGA_CODE << 13) | 0x1422;

  const uint16_t calValues[11] = {
    ATM_PLCONST_H, ATM_PLCONST_L, ATM_LGAIN,
    0x0000, 0x0000, 0x0000,        // Lphi, Ngain, Nphi
    0x08BD, 0x0000,                // PStartTh, PNolTh
    0x0AEC, 0x0000,                // QStartTh, QNolTh
    meterMModeExpected
  };
  const uint16_t adjValues[10] = {
    ATM_UGAIN, ATM_IGAINL, 0x7530,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000
  };

  atmWrite(REG_CALSTART, 0x5678);
  for (uint8_t i = 0; i < 11; i++) atmWrite(REG_CAL_FIRST + i, calValues[i]);
  atmWrite(REG_CS1, atmChecksum(calValues, 11));

  atmWrite(REG_ADJSTART, 0x5678);
  for (uint8_t i = 0; i < 10; i++) atmWrite(REG_ADJ_FIRST + i, adjValues[i]);
  atmWrite(REG_CS2, atmChecksum(adjValues, 10));

  // Any value other than 0x5678 leaves configuration mode and starts metering.
  atmWrite(REG_CALSTART, 0x8765);
  atmWrite(REG_ADJSTART, 0x8765);
  delay(100);

  uint16_t mmode = atmRead(REG_MMODE);
  uint16_t sys = atmRead(REG_SYSSTATUS);

  Serial.print("  MMode readback: 0x");
  Serial.print(mmode, HEX);
  Serial.print(" (expected 0x");
  Serial.print(meterMModeExpected, HEX);
  Serial.print("), SysStatus: 0x");
  Serial.println(sys, HEX);

  if (mmode != meterMModeExpected) {
    Serial.println("  ATM90E26 not responding - check SPI wiring (GP0-3), USEL/MMD/RESET pins and 3V3.");
    return false;
  }
  if ((sys & 0xF000) != 0) {
    Serial.println("  ATM90E26 reports a configuration checksum error.");
    return false;
  }
  Serial.println("  ATM90E26 ready.");
  return true;
}

// Returns false if the IC lost its configuration or stopped responding.
bool readMeter() {
  if (atmRead(REG_MMODE) != meterMModeExpected) return false;

  uint16_t urms  = atmRead(REG_URMS);                // 0.01 V / LSB
  uint16_t irms  = atmRead(REG_IRMS);                // 0.001 A / LSB
  int16_t  pmean = (int16_t)atmRead(REG_PMEAN);      // W, two's complement
  uint16_t pfReg = atmRead(REG_POWERF);              // sign-magnitude, 0.001
  uint16_t freq  = atmRead(REG_FREQ);                // 0.01 Hz / LSB

  meterVoltage = (urms / 100.0f) * VOLTAGE_TRIM;
  meterCurrent = (irms / 1000.0f) * CURRENT_TRIM;
  meterPower   = fabsf((float)pmean) * POWER_TRIM;
  meterFreq    = freq / 100.0f;
  meterPF      = (pfReg & 0x7FFF) / 1000.0f;
  if (meterPF > 1.0f) meterPF = 1.0f;
  return true;
}


// -----------------------------------------------------------------------------
// PLC COMMUNICATION (SoftwareSerial on GP6 RX / GP4 TX, 150 kHz OOK carrier)
// -----------------------------------------------------------------------------
uint8_t calculateCRC8(const uint8_t* data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t j = 0; j < 8; j++) {
      if (crc & 0x80) crc = (crc << 1) ^ 0x07;
      else crc <<= 1;
    }
  }
  return crc;
}

void sendPLCPacket(uint8_t type, uint16_t seq, uint16_t current_mA,
                   uint16_t voltage_dV, uint16_t power_W, uint16_t pf_milli) {
  PLCPacket pkt;
  pkt.header = PLC_HEADER;
  pkt.type = type;
  pkt.seq = seq;
  pkt.timestamp = millis();
  pkt.current_mA = current_mA;
  pkt.voltage_dV = voltage_dV;
  pkt.power_W = power_W;
  pkt.pf_milli = pf_milli;
  pkt.crc = calculateCRC8((const uint8_t*)&pkt, sizeof(pkt) - 1);
  plcSerial.write((const uint8_t*)&pkt, sizeof(pkt));
  plcTxCount++;
}

bool readPLCPacket(PLCPacket& pkt) {
  // Resynchronise on the header byte.
  while (plcSerial.available() > 0 && plcSerial.peek() != PLC_HEADER) {
    plcSerial.read();
  }
  if (plcSerial.available() < (int)sizeof(PLCPacket)) return false;

  plcSerial.readBytes((uint8_t*)&pkt, sizeof(pkt));
  if (calculateCRC8((const uint8_t*)&pkt, sizeof(pkt) - 1) != pkt.crc) {
    plcRxCrcErr++;
    return false;
  }
  return true;
}

// Utility side: answer every valid request with the latest I1 reading.
// The receiver also hears this board's own replies; those are ignored.
void servicePLCUtility() {
  PLCPacket p;
  while (readPLCPacket(p)) {
    if (p.type != PLC_TYPE_REQUEST) continue;
    plcRxOk++;
    lastPlcOkMs = millis();
    sendPLCPacket(PLC_TYPE_REPLY, p.seq,
                  (uint16_t)(meterCurrent * 1000.0f),
                  (uint16_t)(meterVoltage * 10.0f),
                  (uint16_t)meterPower,
                  (uint16_t)(meterPF * 1000.0f));
  }
}

// Customer side: send requests, accept only the reply to the latest request.
// The receiver also hears this board's own requests; those are ignored.
void servicePLCCustomer() {
  unsigned long now = millis();

  if (now - lastPlcRequestMs >= PLC_REQUEST_PERIOD_MS) {
    lastPlcRequestMs = now;
    plcSequence++;
    sendPLCPacket(PLC_TYPE_REQUEST, plcSequence, 0, 0, 0, 0);
  }

  PLCPacket p;
  while (readPLCPacket(p)) {
    if (p.type == PLC_TYPE_REQUEST) continue;          // own echo
    if (p.type == PLC_TYPE_REPLY && p.seq == plcSequence) {
      upstreamCurrent = p.current_mA / 1000.0f;
      upstreamPower = (float)p.power_W;
      lastPlcOkMs = millis();
      plcRxOk++;
    } else {
      plcRxStale++;     // reply with an old sequence ID
    }
  }

  plcLinkOk = (lastPlcOkMs != 0) && (millis() - lastPlcOkMs <= PLC_LINK_TIMEOUT_MS);
}


// -----------------------------------------------------------------------------
// WI-FI + MQTT
// -----------------------------------------------------------------------------
String compactMacAddress() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  return mac;
}

void connectToCurrentWiFi() {
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(WIFI_NETWORKS[wifiNetworkIndex].ssid);
  lastWiFiAttemptMs = millis();
  WiFi.disconnect();
  delay(100);
  WiFi.begin(WIFI_NETWORKS[wifiNetworkIndex].ssid,
             WIFI_NETWORKS[wifiNetworkIndex].password);
}

// If the link drops, retry the network that last worked first; after that
// each failed attempt moves on to the next network in the list.
void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiFailCount = 0;
    return;
  }
  unsigned long now = millis();
  if (now - lastWiFiAttemptMs < WIFI_RETRY_PERIOD_MS) return;

  if (wifiFailCount > 0) {
    wifiNetworkIndex = (wifiNetworkIndex + 1) % WIFI_NETWORK_COUNT;
  }
  wifiFailCount++;
  Serial.println("Wi-Fi not connected - retrying...");
  connectToCurrentWiFi();
}

void maintainMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;
  mqttClient.loop();
  if (mqttClient.connected()) return;

  unsigned long now = millis();
  if (now - lastMqttAttemptMs < MQTT_RETRY_PERIOD_MS) return;
  lastMqttAttemptMs = now;

  String clientId = "26G55-pico2w-" + compactMacAddress();
  Serial.print("Connecting to MQTT broker...");
  if (mqttClient.connect(clientId.c_str())) {
    Serial.println(" connected");
  } else {
    Serial.print(" failed, state=");
    Serial.println(mqttClient.state());
  }
}


// -----------------------------------------------------------------------------
// CUSTOMER SIDE: BYPASS DECISION + PUBLISH
// -----------------------------------------------------------------------------
void updateBypassDecision(float elapsedSeconds) {
  bool anomalyNow = plcLinkOk && (differenceCurrent >= BYPASS_THRESHOLD_A);
  float intervalBypassWh = differencePower * (elapsedSeconds / 3600.0f);

  if (!bypassAlarmActive) {
    if (anomalyNow) {
      candidateBypassEnergyWh += intervalBypassWh;
      anomalyCount++;
      clearCount = 0;
      if (anomalyCount >= ALARM_ON_COUNT) {
        bypassAlarmActive = true;
        possibleBypassEnergyWh += candidateBypassEnergyWh;
        candidateBypassEnergyWh = 0.0f;
        anomalyCount = 0;
      }
    } else {
      anomalyCount = 0;
      candidateBypassEnergyWh = 0.0f;
    }
  } else {
    if (anomalyNow) {
      possibleBypassEnergyWh += intervalBypassWh;
      clearCount = 0;
    } else {
      clearCount++;
      anomalyCount = 0;
      if (clearCount >= ALARM_CLEAR_COUNT) {
        bypassAlarmActive = false;
        clearCount = 0;
      }
    }
  }
}

void publishReading() {
  const char* status = !plcLinkOk ? "PLC LINK LOST"
                      : (bypassAlarmActive ? "POSSIBLE BYPASS" : "NORMAL");

  unsigned long plcAgeMs = (lastPlcOkMs == 0) ? 0 : (millis() - lastPlcOkMs);

  char readingId[96];
  snprintf(readingId, sizeof(readingId), "pico2w-%08lX-%lu", bootNonce, sampleCount);

  char payload[1024];
  snprintf(payload, sizeof(payload),
      "{"
        "\"meter_id\":\"%s\","
        "\"upstream_current_A\":%.3f,"
        "\"main_current_A\":%.3f,"
        "\"difference_A\":%.3f,"
        "\"upstream_power_W\":%.1f,"
        "\"main_power_W\":%.1f,"
        "\"power_difference_W\":%.1f,"
        "\"voltage_V\":%.1f,"
        "\"frequency_Hz\":%.2f,"
        "\"power_factor\":%.3f,"
        "\"metered_energy_Wh\":%.3f,"
        "\"possible_bypass_energy_Wh\":%.3f,"
        "\"plc_ok\":%s,"
        "\"plc_age_ms\":%lu,"
        "\"plc_rx_ok\":%lu,"
        "\"plc_crc_errors\":%lu,"
        "\"reading_id\":\"%s\","
        "\"session_id\":\"%08lX\","
        "\"sample_id\":%lu,"
        "\"status\":\"%s\","
        "\"source\":\"pico2w_atm90e26_plc_mqtt\""
      "}",
      METER_ID,
      upstreamCurrent, mainCurrent, differenceCurrent,
      upstreamPower, mainPower, differencePower,
      meterVoltage, meterFreq, meterPF,
      meteredEnergyWh, possibleBypassEnergyWh,
      plcLinkOk ? "true" : "false",
      plcAgeMs, plcRxOk, plcRxCrcErr,
      readingId, bootNonce, sampleCount, status);

  Serial.println();
  Serial.print("I1 "); Serial.print(upstreamCurrent, 3);
  Serial.print(" A | I2 "); Serial.print(mainCurrent, 3);
  Serial.print(" A | dI "); Serial.print(differenceCurrent, 3);
  Serial.print(" A | "); Serial.print(meterVoltage, 1);
  Serial.print(" V | "); Serial.print(meterFreq, 2);
  Serial.print(" Hz | PF "); Serial.println(meterPF, 3);
  Serial.print("PLC: "); Serial.print(plcLinkOk ? "OK" : "LOST");
  Serial.print(" | age "); Serial.print(plcAgeMs);
  Serial.print(" ms | tx "); Serial.print(plcTxCount);
  Serial.print(" rx "); Serial.print(plcRxOk);
  Serial.print(" crc-err "); Serial.print(plcRxCrcErr);
  Serial.print(" stale "); Serial.println(plcRxStale);
  Serial.print("Status: "); Serial.println(status);

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cloud publish skipped: Wi-Fi unavailable");
    return;
  }
  if (!mqttClient.connected()) {
    Serial.println("Cloud publish skipped: MQTT unavailable");
    return;
  }
  if (mqttClient.publish(MQTT_TOPIC, payload, true)) {
    Serial.println("Published to MQTT");
  } else {
    Serial.println("MQTT publish failed");
  }
}


// -----------------------------------------------------------------------------
// SETUP + LOOP
// -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(1500);

  pinMode(ROLE_PIN, INPUT_PULLUP);
  delay(10);
  isUtilitySide = (digitalRead(ROLE_PIN) == LOW);

  Serial.println();
  Serial.println("26G55 Pico 2 W unified firmware (ATM90E26 + 150 kHz OOK PLC)");
  Serial.println("-------------------------------------------------");
  Serial.print("Role: ");
  Serial.println(isUtilitySide ? "UTILITY SIDE (I1, PLC server)"
                               : "CUSTOMER SIDE (I2, PLC client, MQTT)");

  randomSeed(micros());

  // ATM90E26 SPI0: MISO GP0, CS GP1 (manual), SCK GP2, MOSI GP3
  pinMode(ATM_CS_PIN, OUTPUT);
  digitalWrite(ATM_CS_PIN, HIGH);
  SPI.setRX(ATM_MISO_PIN);
  SPI.setSCK(ATM_SCK_PIN);
  SPI.setTX(ATM_MOSI_PIN);
  SPI.begin(false);
  meterOk = initMeteringIC();
  lastMeterInitMs = millis();

  // PLC OOK: continuous 150 kHz PWM carrier on GP5, TX data on GP4, RX on GP6.
  // Active-low OOK: TX LOW passes the carrier, TX HIGH shunts it (idle).
  pinMode(PLC_TX_PIN, OUTPUT);
  digitalWrite(PLC_TX_PIN, HIGH);
  pinMode(MOD_CARRIER_PIN, OUTPUT);
  analogWriteFreq(PLC_CARRIER_HZ);
  analogWriteRange(PLC_PWM_RANGE);
  analogWrite(MOD_CARRIER_PIN, PLC_PWM_RANGE / 2);
  plcSerial.begin(PLC_BAUD);
  plcSerial.setTimeout(20);

  if (isUtilitySide) {
    Serial.println("Utility side: Wi-Fi off, answering PLC requests.");
    return;
  }

  // ---- Customer side: Wi-Fi then MQTT ----
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setBufferSize(1024);
  mqttClient.setKeepAlive(30);

  WiFi.mode(WIFI_STA);

  // Try each network in order, waiting up to WIFI_SETUP_WAIT_MS for each.
  for (size_t attempt = 0;
       attempt < WIFI_NETWORK_COUNT && WiFi.status() != WL_CONNECTED;
       attempt++) {
    connectToCurrentWiFi();
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_SETUP_WAIT_MS) {
      delay(250);
      Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() != WL_CONNECTED) {
      wifiNetworkIndex = (wifiNetworkIndex + 1) % WIFI_NETWORK_COUNT;
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("Wi-Fi connected to: ");
    Serial.println(WIFI_NETWORKS[wifiNetworkIndex].ssid);
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    wifiFailCount = 0;
  } else {
    Serial.println("Wi-Fi not connected yet. Will keep retrying in the background.");
    wifiFailCount = 1;
  }

  String mac = compactMacAddress();
  unsigned long macHash = 2166136261UL;
  for (size_t i = 0; i < mac.length(); i++) {
    macHash ^= (uint8_t)mac[i];
    macHash *= 16777619UL;
  }
  bootNonce = macHash ^ micros() ^ (unsigned long)random(1, 0x7FFFFFFF);

  lastMqttAttemptMs = millis() - MQTT_RETRY_PERIOD_MS;
  maintainMQTT();
}

void loop() {
  unsigned long now = millis();

  // PLC must be serviced every pass, not only at the sample tick.
  if (isUtilitySide) servicePLCUtility();
  else               servicePLCCustomer();

  if (!isUtilitySide) {
    maintainWiFi();
    maintainMQTT();
  }

  // Retry the metering IC if it failed to start. Nothing is simulated.
  if (!meterOk && now - lastMeterInitMs >= METER_RETRY_MS) {
    lastMeterInitMs = now;
    meterOk = initMeteringIC();
  }

  // ---- Read the ATM90E26 --------------------------------------------------
  if (meterOk && now - lastMeterMs >= METER_PERIOD_MS) {
    unsigned long dtMs = (lastMeterMs == 0) ? METER_PERIOD_MS : (now - lastMeterMs);
    lastMeterMs = now;

    if (!readMeter()) {
      Serial.println("Metering IC lost its configuration - re-initialising");
      meterOk = false;
      lastMeterInitMs = 0;
    } else if (isUtilitySide) {
      upstreamCurrent = meterCurrent;
      upstreamPower = meterPower;
    } else {
      mainCurrent = meterCurrent;
      mainPower = meterPower;
      meteredEnergyWh += mainPower * (dtMs / 3600000.0f);
    }
  }

  // ---- Publish / report every PUBLISH_PERIOD_MS ------------------------------
  if (now - lastPublishMs >= PUBLISH_PERIOD_MS) {
    unsigned long elapsedMs = (lastPublishMs == 0) ? PUBLISH_PERIOD_MS : (now - lastPublishMs);
    lastPublishMs = now;

    if (isUtilitySide) {
      Serial.print("Utility: "); Serial.print(meterVoltage, 1);
      Serial.print(" V, "); Serial.print(meterCurrent, 3);
      Serial.print(" A, "); Serial.print(meterPower, 1);
      Serial.print(" W, "); Serial.print(meterFreq, 2);
      Serial.print(" Hz | PLC requests answered: "); Serial.print(plcRxOk);
      Serial.print(", crc-err "); Serial.println(plcRxCrcErr);
    } else if (!meterOk) {
      Serial.println("Metering IC not ready - nothing published");
    } else {
      if (plcLinkOk) {
        differenceCurrent = fabsf(upstreamCurrent - mainCurrent);
        differencePower = fabsf(upstreamPower - mainPower);
      } else {
        // Link lost is not theft: show I1 = I2 so no false bypass is drawn.
        upstreamCurrent = mainCurrent;
        upstreamPower = mainPower;
        differenceCurrent = 0.0f;
        differencePower = 0.0f;
      }
      sampleCount++;
      updateBypassDecision(elapsedMs / 1000.0f);
      publishReading();
    }
  }
}
