#include "config_store.h"

#include <algorithm>
#include <cmath>
#include <cstring>

#ifndef ARDUINO
#include <iomanip>
#include <iostream>
#else
#include <Arduino.h>
#include <Preferences.h>
#include "motion.h"
#endif

namespace ConfigStore {
namespace {

constexpr uint32_t MAGIC   = 0x484D4350; ///< «HMCP» — Home Motion Config Persistent.
constexpr uint32_t VERSION = 3;          ///< Версия формата сохраняемых данных (3 — добавлены тримы duty для прямого и обратного хода).

struct PersistentBlob {
  uint32_t magic = MAGIC;
  uint32_t version = VERSION;
  TuningConfig cfg{};
  uint32_t checksum = 0;
};

#ifndef ARDUINO
TuningConfig gStoredCfg = defaults(); ///< Имитация энергонезависимого хранилища в юнит-тестах.
bool gHasStored = false;              ///< Флаг, что конфигурация была «сохранена» во время теста.
#endif

Environment gEnv{};                 ///< Текущее окружение (наличие гироскопа).
TuningConfig gRuntime = defaults(); ///< Рабочая конфигурация, используемая движением.

/// Простая контрольная сумма (xor + сумма), чтобы отфильтровать повреждённые данные.
uint32_t compute_checksum(const TuningConfig& cfg) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&cfg);
  uint32_t sum = 0;
  uint32_t xorv = 0;
  for (size_t i = 0; i < sizeof(TuningConfig); ++i) {
    sum += bytes[i];
    xorv ^= (static_cast<uint32_t>(bytes[i]) << ((i % 4) * 8));
  }
  return sum ^ xorv ^ 0xA5A5A5A5u;
}

#ifdef ARDUINO
Preferences gPrefs;           ///< Набор настроек в NVS.
bool gPrefsOpened = false;    ///< Ленивая инициализация Preferences.

bool ensure_prefs() {
  if (!gPrefsOpened) {
    gPrefsOpened = gPrefs.begin("motion", false);
    if (!gPrefsOpened) {
      Serial.println(F("[CONFIG] ошибка: не удалось открыть раздел NVS 'motion'"));
    }
  }
  return gPrefsOpened;
}
#endif

} // namespace

TuningConfig defaults() {
  TuningConfig cfg{};
  return cfg;
}

void set_environment(const Environment& env) {
  gEnv = env;
#ifdef ARDUINO
  Serial.printf("[CONFIG] окружение обновлено: gyro=%s addr=0x%02X\n",
                gEnv.gyro_available ? "yes" : "no",
                gEnv.mpu_i2c_addr);
#else
  std::cout << "[CONFIG] environment updated: gyro="
            << (gEnv.gyro_available ? "yes" : "no")
            << " addr=0x" << std::hex << static_cast<int>(gEnv.mpu_i2c_addr)
            << std::dec << std::endl;
#endif
}

Environment environment() {
  return gEnv;
}

void set_runtime_config(const TuningConfig& cfg) {
  gRuntime = cfg;
#ifdef ARDUINO
  Serial.println(F("[CONFIG] рабочая конфигурация обновлена в ОЗУ"));
#endif
}

const TuningConfig& current_config() {
  return gRuntime;
}

void apply_tuning_to_params(const TuningConfig& cfg,
                            Motion::Params& params,
                            const Environment& env) {
  // --- Геометрия и одометрия ---
  params.wheel_d_mm      = cfg.wheel_diameter_mm;
  params.wheel_base_mm   = cfg.wheel_base_mm;
  params.tpr_left        = std::max(1L, cfg.tpr_left);
  params.tpr_right       = std::max(1L, cfg.tpr_right);
  params.enc_left_is_enc1 = cfg.enc_left_is_enc1;

  // --- Коэффициенты прямолинейного хода ---
  params.kp_straight_enc  = cfg.kp_straight_enc;
  params.kp_straight_gyro = cfg.kp_straight_gyro;
  params.ki_straight_gyro = cfg.ki_straight_gyro;
  params.duty_trim_forward  = cfg.duty_trim_forward;
  params.duty_trim_backward = cfg.duty_trim_backward;

  // --- Коэффициенты разворота ---
  params.kp_turn_enc  = cfg.kp_turn_enc;
  params.kp_turn_gyro = cfg.kp_turn_gyro;
  params.ki_turn_gyro = cfg.ki_turn_gyro;

  // --- Ограничения и защита от застревания ---
  params.heading_tolerance_deg    = cfg.heading_tolerance_deg;
  params.stuck_rate_threshold_dps = cfg.stuck_rate_threshold_dps;
  params.stuck_timeout_ms         = cfg.stuck_timeout_ms;
  params.yaw_integral_limit       = cfg.yaw_integral_limit;

  // --- Включаем гироскоп только если и пользователь разрешил, и датчик найден ---
  params.enable_gyro_feedback = cfg.enable_gyro_feedback && env.gyro_available && env.mpu_i2c_addr != 0;
  params.mpu_i2c_addr         = params.enable_gyro_feedback ? env.mpu_i2c_addr : 0;
  params.use_gyro_for_straight = params.enable_gyro_feedback && cfg.use_gyro_for_straight; // Активируем yaw только при реально доступном гироскопе.
}

bool load_from_storage(TuningConfig& out) {
#ifdef ARDUINO
  if (!ensure_prefs()) {
    return false;
  }
  const size_t expected = sizeof(PersistentBlob);
  size_t stored = gPrefs.getBytesLength("tuning");
  if (stored != expected) {
    return false;
  }
  PersistentBlob blob{};
  if (gPrefs.getBytes("tuning", &blob, expected) != expected) {
    return false;
  }
  if (blob.magic != MAGIC || blob.version != VERSION) {
    Serial.println(F("[CONFIG] предупреждение: версия конфигурации не совпадает"));
    return false;
  }
  const uint32_t crc = compute_checksum(blob.cfg);
  if (crc != blob.checksum) {
    Serial.println(F("[CONFIG] предупреждение: контрольная сумма не сходится, используем значения по умолчанию"));
    return false;
  }
  out = blob.cfg;
  return true;
#else
  if (!gHasStored) {
    return false;
  }
  out = gStoredCfg;
  return true;
#endif
}

bool save_to_storage(const TuningConfig& cfg) {
#ifdef ARDUINO
  if (!ensure_prefs()) {
    return false;
  }
  PersistentBlob blob{};
  blob.magic = MAGIC;
  blob.version = VERSION;
  blob.cfg = cfg;
  blob.checksum = compute_checksum(cfg);
  const size_t expected = sizeof(PersistentBlob);
  size_t written = gPrefs.putBytes("tuning", &blob, expected);
  if (written != expected) {
    Serial.println(F("[CONFIG] ошибка: не удалось сохранить параметры в NVS"));
    return false;
  }
  Serial.println(F("[CONFIG] конфигурация сохранена в NVS"));
  return true;
#else
  gStoredCfg = cfg;
  gHasStored = true;
  return true;
#endif
}

TuningConfig load_or_defaults() {
  TuningConfig cfg = defaults();
  TuningConfig stored{};
  if (load_from_storage(stored)) {
    cfg = stored;
  }
  set_runtime_config(cfg);
  return cfg;
}

bool reconfigure_motion(const TuningConfig& cfg) {
  set_runtime_config(cfg);
#ifdef ARDUINO
  Motion::Params params = Motion::params();
  float previousHeading = Motion::current_heading_deg();
  apply_tuning_to_params(cfg, params, gEnv);
  Serial.println(F("[CONFIG] применяем обновлённые параметры к Motion"));
  bool runtimeOk = Motion::update_runtime(params);
  if (!runtimeOk) {
    Serial.println(F("[CONFIG] предупреждение: runtime-обновление не удалось, выполняем полную инициализацию"));
    if (!Motion::init(params)) {
      Serial.println(F("[CONFIG] ошибка: Motion::init() вернул false при применении параметров"));
      return false;
    }
  }
  Serial.println(F("[CONFIG] Motion получил обновлённые коэффициенты"));
  if (params.enable_gyro_feedback) {
    Motion::reset_heading(previousHeading);
  }
#endif
  return true;
}

bool update_and_persist(const TuningConfig& cfg) {
  if (!reconfigure_motion(cfg)) {
    return false;
  }
  set_runtime_config(cfg);
  return save_to_storage(cfg);
}

} // namespace ConfigStore

