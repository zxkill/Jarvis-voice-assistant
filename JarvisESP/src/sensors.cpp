#include "sensors.h"

// ---------- I2C helpers ----------
static bool i2c_write_u8(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return (Wire.endTransmission() == 0);
}
static bool i2c_read_u8(uint8_t addr, uint8_t reg, uint8_t& val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, 1) != 1) return false;
  val = Wire.read();
  return true;
}
static bool i2c_read_u16(uint8_t addr, uint8_t reg, uint16_t& val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, 2) != 2) return false;
  uint8_t msb = Wire.read();
  uint8_t lsb = Wire.read();
  val = (uint16_t(msb) << 8) | lsb;
  return true;
}
static bool i2c_read_n(uint8_t addr, uint8_t reg, uint8_t* buf, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, (int)n) != (int)n) return false;
  for (size_t i=0;i<n;i++) buf[i]=Wire.read();
  return true;
}

bool i2c_begin(int sda, int scl, uint32_t freq) {
  Wire.begin(sda, scl, freq);
  delay(10);
  return true;
}

I2CScanResult i2c_scan_sensors() {
  I2CScanResult res;
  // Скан INA219 (часто 0x40..0x4F, дефолт 0x40)
  for (uint8_t a=0x40; a<=0x4F; ++a) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission()==0) { res.ina219_addr=a; break; }
  }
  // Скан MPU (0x68/0x69)
  for (uint8_t a: {uint8_t(0x68), uint8_t(0x69)}) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission()==0) { res.mpu_addr=a; break; }
  }
  return res;
}

// ---------- INA219 ----------
/*
  Регистр карта:
  CONFIG(0x00), SHUNT_V(0x01), BUS_V(0x02), POWER(0x03), CURRENT(0x04), CAL(0x05)
  Калибровка: Cal = 0.04096 / (Current_LSB * Rshunt)
  Power_LSB = 20 * Current_LSB
*/
static float g_INA_currentLSB_A = 0.0001f; // 100 µA/LSB (по умолчанию)
static float g_INA_powerLSB_W   = 0.0020f; // 20 * 100µA = 2 mW/LSB

bool ina219_init(uint8_t addr, float Rshunt_ohm, float I_max_A) {
  // Выбор тока LSB (грубая оценка) — возьмём Imax/32767, но округлим вверх к удобной ступени.
  float curLSB = I_max_A / 32767.0f; // A/LSB
  // округление к 100µA шагу
  if (curLSB < 0.0001f) curLSB = 0.0001f; // не меньше 100µA/LSB
  g_INA_currentLSB_A = curLSB;
  g_INA_powerLSB_W   = 20.0f * g_INA_currentLSB_A;

  // Cal register
  uint16_t cal = (uint16_t)(0.04096f / (g_INA_currentLSB_A * Rshunt_ohm));
  if (cal == 0) cal = 1;

  // CONFIG: Bus 32V, Gain /8 (320mV), 12-bit, 128 samples, continuous (пример)
  uint16_t config = 0x019F; // BRNG=32V (1<<13), PG=320mV (11<<11), BADC=1111, SADC=1111, Mode=111
  // Запись CAL и CONFIG
  Wire.beginTransmission(addr);
  Wire.write(0x05); Wire.write((uint8_t)(cal>>8)); Wire.write((uint8_t)(cal&0xFF));
  if (Wire.endTransmission()!=0) return false;

  Wire.beginTransmission(addr);
  Wire.write(0x00); Wire.write((uint8_t)(config>>8)); Wire.write((uint8_t)(config&0xFF));
  if (Wire.endTransmission()!=0) return false;

  // Небольшой лог
  Serial.printf("[INA] addr=0x%02X Rshunt=%.3fΩ I_LSB=%.6fA Cal=0x%04X\n",
                addr, Rshunt_ohm, g_INA_currentLSB_A, cal);
  return true;
}

bool ina219_read(uint8_t addr, INA219Reading& out) {
  uint16_t raw_shunt=0, raw_bus=0, raw_power=0, raw_current=0;
  if (!i2c_read_u16(addr, 0x01, raw_shunt))  return false;
  if (!i2c_read_u16(addr, 0x02, raw_bus))    return false;
  if (!i2c_read_u16(addr, 0x03, raw_power))  return false;
  if (!i2c_read_u16(addr, 0x04, raw_current))return false;

  // Шунт: signed, 10uV/LSB
  int16_t shunt_signed = (int16_t)raw_shunt;
  out.shunt_mV = (float)shunt_signed * 0.01f; // 10uV -> mV

  // Шина: 1 LSB = 4 mV, [0..15] — статус
  out.bus_V = (float)((raw_bus >> 3) * 4) / 1000.0f;

  // Ток/мощность по калибровке
  int16_t cur_signed = (int16_t)raw_current;
  out.current_A = float(cur_signed) * g_INA_currentLSB_A;
  out.power_W   = (float)raw_power * g_INA_powerLSB_W;

  return true;
}

// ---------- MPU-9250/6500 ----------
/*
  Базовая инициализация (общая для 6500/9250):
    PWR_MGMT_1 = 0x01 (авточасы PLL)
    CONFIG = 0x03 (DLPF)
    GYRO_CONFIG = 0x10 (±1000 dps)
    ACCEL_CONFIG = 0x08 (±4g)
    ACCEL_CONFIG2 = 0x03 (DLPF для акселя)
  Конверсии:
    gyro LSB = 32.8 LSB/(°/s) для ±1000
    accel LSB = 8192 LSB/g для ±4g
    temp °C = raw/333.87 + 21.0
*/
static const uint8_t REG_SMPLRT_DIV   = 0x19;
static const uint8_t REG_CONFIG       = 0x1A;
static const uint8_t REG_GYRO_CONFIG  = 0x1B;
static const uint8_t REG_ACCEL_CONFIG = 0x1C;
static const uint8_t REG_ACCEL_CONFIG2= 0x1D;
static const uint8_t REG_INT_PIN_CFG  = 0x37;
static const uint8_t REG_ACCEL_XOUT_H = 0x3B;
static const uint8_t REG_TEMP_OUT_H   = 0x41;
static const uint8_t REG_GYRO_XOUT_H  = 0x43;
static const uint8_t REG_PWR_MGMT_1   = 0x6B;
static const uint8_t REG_WHO_AM_I     = 0x75; // 0x71 (6500), 0x70/0x71/0x73/0x68 иногда

bool mpu_init(uint8_t addr) {
  uint8_t who=0;
  if (!i2c_read_u8(addr, REG_WHO_AM_I, who)) {
    Serial.printf("[MPU] addr=0x%02X WHOAMI read fail\n", addr);
    return false;
  }
  Serial.printf("[MPU] addr=0x%02X WHOAMI=0x%02X\n", addr, who);

  // Сброс сна и выбор PLL
  if (!i2c_write_u8(addr, REG_PWR_MGMT_1, 0x01)) return false;
  delay(10);

  // Фильтры/диапазоны
  if (!i2c_write_u8(addr, REG_CONFIG,        0x03)) return false;
  if (!i2c_write_u8(addr, REG_GYRO_CONFIG,   0x10)) return false; // ±1000 dps
  if (!i2c_write_u8(addr, REG_ACCEL_CONFIG,  0x08)) return false; // ±4g
  if (!i2c_write_u8(addr, REG_ACCEL_CONFIG2, 0x03)) return false;

  // Частота выборки (доп.): делитель = 4 → ~200 Гц при базовом 1кГц
  if (!i2c_write_u8(addr, REG_SMPLRT_DIV, 4)) return false;

  // Лог
  Serial.printf("[MPU] init OK @0x%02X (±4g, ±1000dps, DLPF=3, SR=~200Hz)\n", addr);
  return true;
}

bool mpu_read(uint8_t addr, MPUReading& out) {
  uint8_t buf[14] = {0}; // accel(6) + temp(2) + gyro(6)
  if (!i2c_read_n(addr, REG_ACCEL_XOUT_H, buf, 14)) return false;

  auto rd = [&](int idx)->int16_t {
    return (int16_t)((buf[idx] << 8) | buf[idx+1]);
  };
  int16_t ax = rd(0), ay = rd(2), az = rd(4);
  int16_t t  = rd(6);
  int16_t gx = rd(8), gy = rd(10), gz = rd(12);

  const float A_LSB = 8192.0f; // ±4g
  const float G_LSB = 32.8f;   // ±1000 dps

  out.ax_g = ax / A_LSB;
  out.ay_g = ay / A_LSB;
  out.az_g = az / A_LSB;

  out.gx_dps = gx / G_LSB;
  out.gy_dps = gy / G_LSB;
  out.gz_dps = gz / G_LSB;

  out.temp_C = (float)t / 333.87f + 21.0f;
  return true;
}
