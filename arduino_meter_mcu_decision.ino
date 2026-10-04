/*
  26G55 Smart Energy Meter - MCU-side bypass decision simulator
  -------------------------------------------------------------
  The Arduino performs the bypass decision locally and now sends
  both current and power measurements for dashboard versatility.

  It sends this CSV over USB Serial every 3 seconds:

      upstream_A,main_A,difference_A,upstream_W,main_W,power_difference_W,voltage_V,power_factor,metered_energy_Wh,possible_bypass_energy_Wh,session_id,sample_id,status

  Example:
      1.198,1.187,0.011,274.1,271.6,2.5,229.9,0.995,0.226,0.000,123456,1,NORMAL
      1.207,0.623,0.584,276.4,142.7,133.7,230.1,0.995,3.842,0.334,123456,19,POSSIBLE BYPASS

  IMPORTANT:
  - The Python bridge does not decide NORMAL/POSSIBLE BYPASS.
  - The MCU applies the prototype current-mismatch threshold and persistence logic.
  - The dashboard only displays the status reported by the MCU.
  - Power is included as an additional measurement/view; it does not change
    the current-based prototype bypass decision.
  - Potential unmetered energy is accumulated only for a bypass condition that
    passes the same persistence rule. Energy from the first anomalous samples is
    held temporarily and committed only when POSSIBLE BYPASS is confirmed.
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

// A normal meter-style cumulative energy register for the main meter path.
// This demo integrates main active power over time. Final hardware should read
// accumulated energy from the ATM90E26.
float meteredEnergyWh = 0.0;

// Energy associated only with validated possible-bypass periods.
// candidateBypassEnergyWh is temporary until ALARM_ON_COUNT is reached.
float candidateBypassEnergyWh = 0.0;
float possibleBypassEnergyWh = 0.0;

// Each reading carries a boot-session ID plus monotonically increasing sample ID.
// The cloud combines these into a unique reading_id so a retained MQTT packet
// cannot be inserted again with a fresh timestamp after a reconnect.
unsigned long sessionId = 0;

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


void updateBypassDecisionAndEnergy(float elapsedSeconds) {
  bool anomalyNow = (differenceCurrent >= BYPASS_THRESHOLD_A);

  // Normal meter energy: continuously integrate the power that the main meter sees.
  // Wh = W x hours. This is session energy for the Arduino demonstration.
  meteredEnergyWh += mainPower * (elapsedSeconds / 3600.0);

  // Wh = W x hours. This is demo-only numerical integration of the
  // simulated power difference. Final hardware should use metering data
  // from the ATM90E26 for the energy quantity.
  float intervalEnergyWh = differencePower * (elapsedSeconds / 3600.0);

  if (!bypassAlarmActive) {
    if (anomalyNow) {
      // Hold energy from the candidate event temporarily. This ensures that
      // when the third consecutive anomaly confirms the event, energy from
      // the first two anomalous intervals is not lost.
      candidateBypassEnergyWh += intervalEnergyWh;
      anomalyCount++;
      clearCount = 0;

      if (anomalyCount >= ALARM_ON_COUNT) {
        bypassAlarmActive = true;

        // Commit the entire validated candidate event to the session total.
        possibleBypassEnergyWh += candidateBypassEnergyWh;
        candidateBypassEnergyWh = 0.0;
        anomalyCount = 0;
      }
    } else {
      // A short spike that did not pass the persistence rule is rejected,
      // including its temporary energy.
      anomalyCount = 0;
      candidateBypassEnergyWh = 0.0;
    }
  } else {
    if (anomalyNow) {
      // Once validated, accumulate only while the electrical anomaly is
      // actually present. Do not continue during the alarm-clear delay.
      possibleBypassEnergyWh += intervalEnergyWh;
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


void sendReading() {
  // Exact CSV expected by serial_to_mqtt.py:
  // upstream_A,main_A,difference_A,upstream_W,main_W,power_difference_W,voltage_V,power_factor,metered_energy_Wh,possible_bypass_energy_Wh,session_id,sample_id,status
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
  Serial.print(meteredEnergyWh, 3);
  Serial.print(",");
  Serial.print(possibleBypassEnergyWh, 3);
  Serial.print(",");
  Serial.print(sessionId);
  Serial.print(",");
  Serial.print(sampleCount);
  Serial.print(",");
  Serial.println(bypassAlarmActive ? "POSSIBLE BYPASS" : "NORMAL");
}


void setup() {
  Serial.begin(115200);

  // Floating analogue input gives enough variation for a demo seed.
  randomSeed(analogRead(A0) ^ micros());
  sessionId = ((unsigned long)random(100000L, 999999L) << 8) ^ micros() ^ analogRead(A0);

  delay(1000);
}


void loop() {
  unsigned long now = millis();

  if (now - lastSample >= SAMPLE_PERIOD_MS) {
    unsigned long elapsedMs = (lastSample == 0) ? SAMPLE_PERIOD_MS : (now - lastSample);
    lastSample = now;

    generateMeasurements();
    updateBypassDecisionAndEnergy(elapsedMs / 1000.0);
    sendReading();
  }
}
