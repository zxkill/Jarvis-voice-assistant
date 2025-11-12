#include "config_store.h"
#include "motion_params.h"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

void test_defaults() {
  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  assert(std::fabs(cfg.wheel_diameter_mm - 71.5f) < 1e-3f);
  assert(cfg.tpr_left == 1368);
  assert(cfg.enable_gyro_feedback);
  assert(cfg.use_gyro_for_straight);
  assert(std::fabs(cfg.duty_trim_forward) < 1e-3f);
  assert(std::fabs(cfg.duty_trim_backward) < 1e-3f);
}

void test_apply_with_gyro() {
  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  cfg.wheel_diameter_mm = 72.5f;
  cfg.wheel_base_mm = 240.0f;
  cfg.tpr_left = 1500;
  cfg.tpr_right = 1520;
  cfg.kp_turn_gyro = 11.5f;
  cfg.yaw_integral_limit = 55.0f;
  cfg.use_gyro_for_straight = true;
  cfg.duty_trim_forward = 6.5f;
  cfg.duty_trim_backward = -4.0f;

  Motion::Params params{};
  params.enable_gyro_feedback = false; // будет обновлено функцией

  ConfigStore::Environment env{};
  env.gyro_available = true;
  env.mpu_i2c_addr = 0x68;

  ConfigStore::apply_tuning_to_params(cfg, params, env);

  assert(std::fabs(params.wheel_d_mm - 72.5f) < 1e-3f);
  assert(params.tpr_left == 1500);
  assert(params.tpr_right == 1520);
  assert(std::fabs(params.kp_turn_gyro - 11.5f) < 1e-3f);
  assert(std::fabs(params.yaw_integral_limit - 55.0f) < 1e-3f);
  assert(params.enable_gyro_feedback);
  assert(params.mpu_i2c_addr == 0x68);
  assert(params.use_gyro_for_straight);
  assert(std::fabs(params.duty_trim_forward - 6.5f) < 1e-3f);
  assert(std::fabs(params.duty_trim_backward + 4.0f) < 1e-3f);
}

void test_apply_without_gyro() {
  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  cfg.enable_gyro_feedback = true;

  Motion::Params params{};
  params.enable_gyro_feedback = true;

  ConfigStore::Environment env{};
  env.gyro_available = false;

  ConfigStore::apply_tuning_to_params(cfg, params, env);
  assert(!params.enable_gyro_feedback);
  assert(params.mpu_i2c_addr == 0);
  assert(!params.use_gyro_for_straight);
}

void test_apply_preserves_hardware_fields() {
  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  cfg.kp_straight_enc = 1.05f; // изменяем мягкие параметры

  Motion::Params params{};
  params.pwm_freq_hz = 7777;
  params.pwm_res_bits = 9;
  params.pin_left_pwm_fwd = 2;
  params.pin_left_pwm_back = 15;
  params.pin_right_pwm_fwd = 13;
  params.pin_right_pwm_back = 12;
  params.use_left_enable = true;
  params.pin_left_en_a = 25;
  params.pin_left_en_b = 26;

  ConfigStore::Environment env{};
  env.gyro_available = true;
  env.mpu_i2c_addr = 0x68;

  ConfigStore::apply_tuning_to_params(cfg, params, env);

  assert(params.pwm_freq_hz == 7777);
  assert(params.pwm_res_bits == 9);
  assert(params.pin_left_pwm_fwd == 2);
  assert(params.pin_left_pwm_back == 15);
  assert(params.pin_right_pwm_fwd == 13);
  assert(params.pin_right_pwm_back == 12);
  assert(params.use_left_enable);
  assert(params.pin_left_en_a == 25);
  assert(params.pin_left_en_b == 26);
  assert(params.use_gyro_for_straight);
}

void test_storage_roundtrip() {
  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  cfg.kp_straight_enc = 1.23f;
  cfg.tpr_left = 2048;
  cfg.use_gyro_for_straight = false;
  cfg.duty_trim_forward = 3.0f;
  cfg.duty_trim_backward = -2.0f;
  assert(ConfigStore::save_to_storage(cfg));

  ConfigStore::TuningConfig loaded{};
  assert(ConfigStore::load_from_storage(loaded));
  assert(std::fabs(loaded.kp_straight_enc - 1.23f) < 1e-3f);
  assert(loaded.tpr_left == 2048);
  assert(!loaded.use_gyro_for_straight);
  assert(std::fabs(loaded.duty_trim_forward - 3.0f) < 1e-3f);
  assert(std::fabs(loaded.duty_trim_backward + 2.0f) < 1e-3f);
}

void test_update_and_persist() {
  ConfigStore::Environment env{};
  env.gyro_available = true;
  env.mpu_i2c_addr = 0x68;
  ConfigStore::set_environment(env);

  ConfigStore::TuningConfig cfg = ConfigStore::defaults();
  cfg.wheel_diameter_mm = 69.9f;
  cfg.enable_gyro_feedback = true;
  cfg.duty_trim_backward = 1.5f;

  assert(ConfigStore::update_and_persist(cfg));
  const auto& runtime = ConfigStore::current_config();
  assert(std::fabs(runtime.wheel_diameter_mm - 69.9f) < 1e-3f);
  assert(std::fabs(runtime.duty_trim_backward - 1.5f) < 1e-3f);
}

} // namespace

int main() {
  test_defaults();
  test_apply_with_gyro();
  test_apply_without_gyro();
  test_apply_preserves_hardware_fields();
  test_storage_roundtrip();
  test_update_and_persist();

  std::cout << "Config store tests passed" << std::endl;
  return 0;
}

