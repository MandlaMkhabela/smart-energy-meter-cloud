/*
  26G55 Smart Energy Meter - MCU-side bypass decision simulator
  -------------------------------------------------------------
  The Arduino performs the bypass decision locally and now sends
  both current and power measurements for dashboard versatility.

  It sends this CSV over USB Serial every 3 seconds:

      upstream_A,main_A,difference_A,upstream_W,main_W,power_difference_W,voltage_V,power_factor,status

  Example:
      1.198,1.187,0.011,274.1,271.6,2.5,229.9,0.995,NORMAL
      1.207,0.623,0.584,276.4,142.7,133.7,230.1,0.995,POSSIBLE_BYPASS

  IMPORTANT:
  - The Python bridge does not decide NORMAL/POSSIBLE_BYPASS.
  - The MCU applies the prototype current-mismatch threshold and persistence logic.
  - The dashboard only displays the status reported by the MCU.
  - Power is included as an additional measurement/view; it does not change
    the current-based prototype bypass decision.
  - In this Arduino demo, power is simulated from V x I x PF. In the final
    Pico 2 W + ATM90E26 implementation, replace these simulated values with
    the metering IC's measured voltage/current/active-power/PF values.
*/

const unsigned long SAMPLE_PERIOD_MS = 3000;

// Prototype detection settings.
// At ~1.2 A load, 0.25 A is a deliberately large mismatch relative
// to normal sensor/measurement noise. This is a demo threshold, not
// a final calibrated field threshold.
const float BYPASS_THRESHOLD_A = 0.25;

// Require persistence before raising/clearing the alarm.
const byte ALARM_ON_COUNT = 3;     // 3 consecutive anomalous samples
const byte ALARM_CLEAR_COUNT = 2;  // 2 consecutive normal samples

unsigned long lastSample = 0;
unsigned long sampleCount = 0;

float loadCurrent = 1.195;
float upstreamCurrent = 0.0;
float mainCurrent = 0.0;
float differenceCurrent = 0.0;

float lineVoltage = 230.0;
float powerFactor = 0.995;
float upstreamPower = 0.0;
float mainPower = 0.0;
float differencePower = 0.0;

byte anomalyCount = 0;
byte clearCount = 0;
bool bypassAlarmActive = false;


// Small floating-point random offset in +/- amplitude.
float noise(float amplitude) {
  long r = random(-1000, 1001);
  return (r / 1000.0) * amplitude;
}


void generateMeasurements() {
  sampleCount++;

  // Slowly varying load instead of unrelated random values.
  loadCurrent += noise(0.010);

  if (loadCurrent < 1.155) loadCurrent = 1.155;
  if (loadCurrent > 1.235) loadCurrent = 1.235;

  // Small realistic variation around nominal 230 V and near-unity PF
  // for the resistive incandescent test load.
  lineVoltage += noise(0.35);
  if (lineVoltage < 228.5) lineVoltage = 228.5;
  if (lineVoltage > 231.5) lineVoltage = 231.5;

  powerFactor += noise(0.0010);
  if (powerFactor < 0.990) powerFactor = 0.990;
  if (powerFactor > 1.000) powerFactor = 1.000;

  /*
    Demo cycle:
      samples  1-16 : normal
      samples 17-22 : partial-bypass condition
      samples 23-24 : normal recovery

    Then the 24-sample cycle repeats.

    During partial bypass, the upstream sensor still sees the whole load
    but the main meter sees only about 50-58% of it.
  */
  byte phase = ((sampleCount - 1) % 24) + 1;
  bool simulatedPartialBypass = (phase >= 17 && phase <= 22);

  if (simulatedPartialBypass) {
    upstreamCurrent = loadCurrent + noise(0.010);

    float meteredFraction = 0.54 + noise(0.035);
    mainCurrent = loadCurrent * meteredFraction + noise(0.010);
  } else {
    upstreamCurrent = loadCurrent + noise(0.010);
    mainCurrent = loadCurrent + noise(0.012);
  }

  if (upstreamCurrent < 0.0) upstreamCurrent = 0.0;
  if (mainCurrent < 0.0) mainCurrent = 0.0;

  differenceCurrent = abs(upstreamCurrent - mainCurrent);

  // Demo-only power calculation. Final hardware should use ATM90E26 active power.
  upstreamPower = lineVoltage * upstreamCurrent * powerFactor;
  mainPower = lineVoltage * mainCurrent * powerFactor;
  differencePower = abs(upstreamPower - mainPower);
}


void updateBypassDecision() {
  bool anomalyNow = (differenceCurrent >= BYPASS_THRESHOLD_A);

  if (!bypassAlarmActive) {
    if (anomalyNow) {
      anomalyCount++;
      clearCount = 0;

      if (anomalyCount >= ALARM_ON_COUNT) {
        bypassAlarmActive = true;
        anomalyCount = 0;
      }
    } else {
      anomalyCount = 0;
    }
  } else {
    if (!anomalyNow) {
      clearCount++;
      anomalyCount = 0;

      if (clearCount >= ALARM_CLEAR_COUNT) {
        bypassAlarmActive = false;
        clearCount = 0;
      }
    } else {
      clearCount = 0;
    }
  }
}


void sendReading() {
  // Exact CSV expected by serial_to_mqtt.py:
  // upstream_A,main_A,difference_A,upstream_W,main_W,power_difference_W,voltage_V,power_factor,status
  Serial.print(upstreamCurrent, 3);
  Serial.print(",");
  Serial.print(mainCurrent, 3);
  Serial.print(",");
  Serial.print(differenceCurrent, 3);
  Serial.print(",");
  Serial.print(upstreamPower, 1);
  Serial.print(",");
  Serial.print(mainPower, 1);
  Serial.print(",");
  Serial.print(differencePower, 1);
  Serial.print(",");
  Serial.print(lineVoltage, 1);
  Serial.print(",");
  Serial.print(powerFactor, 3);
  Serial.print(",");
  Serial.println(bypassAlarmActive ? "POSSIBLE_BYPASS" : "NORMAL");
}


void setup() {
  Serial.begin(115200);

  // Floating analogue input gives enough variation for a demo seed.
  randomSeed(analogRead(A0));

  delay(1000);
}


void loop() {
  if (millis() - lastSample >= SAMPLE_PERIOD_MS) {
    lastSample = millis();

    generateMeasurements();
    updateBypassDecision();
    sendReading();
  }
}
