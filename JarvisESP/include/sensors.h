#pragma once
#include <Arduino.h>
#include <Wire.h>

// ---- Общие ----
struct I2CScanResult {
  uint8_t ina219_addr = 0x00;
  uint8_t mpu_addr    = 0x00; // 0x68 или 0x69
};

// ---- INA219 ----
struct INA219Reading {
  float shunt_mV = NAN;
  float bus_V    = NAN;
  float current_A= NAN;
  float power_W  = NAN;
};

bool     i2c_begin(int sda=21, int scl=22, uint32_t freq=400000);
I2CScanResult i2c_scan_sensors();

// Инициализация INA219: Rshunt в Омах (обычно 0.1Ω), I_max для выбора LSB (A)
bool     ina219_init(uint8_t addr, float Rshunt_ohm=0.1f, float I_max_A=3.2f);
bool     ina219_read(uint8_t addr, INA219Reading& out);

// ---- MPU-9250/6500 (только акс/гиро/температура; без магнетометра) ----
struct MPUReading {
  float ax_g=NAN, ay_g=NAN, az_g=NAN;
  float gx_dps=NAN, gy_dps=NAN, gz_dps=NAN;
  float temp_C=NAN;
};

bool     mpu_init(uint8_t addr);
bool     mpu_read(uint8_t addr, MPUReading& out);
