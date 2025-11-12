#pragma once

#include <stdint.h>

namespace Motion {

/**
 * \brief Параметры системы движения.
 *
 * Вынесены в отдельный заголовок без зависимостей от Arduino, чтобы их можно
 * было использовать в юнит-тестах и модулях конфигурации.
 */
struct Params {
  // Геометрия
  float wheel_d_mm      = 70.5f;   // диаметр колеса
  float wheel_base_mm   = 235.0f;  // база (расстояние между центрами колёс)

  // Одометрия
  long  tpr_left        = 1470;    // ticks per revolution (левое)
  long  tpr_right       = 1470;    // ticks per revolution (правое)

  // Привязка энкодеров
  bool  enc_left_is_enc1 = true;   // true: ENC1=левый, ENC2=правый

  // PWM
  int   pwm_freq_hz     = 15000;
  int   pwm_res_bits    = 10;      // 8..12

  // Пины PWM
  int   pin_left_pwm_fwd  = -1;
  int   pin_left_pwm_back = -1;
  int   pin_right_pwm_fwd = -1;
  int   pin_right_pwm_back= -1;

  // EN пины
  bool  use_left_enable   = false;
  int   pin_left_en_a     = -1;
  int   pin_left_en_b     = -1;

  // Параметры коррекции по гироскопу
  bool     enable_gyro_feedback      = false;
  uint8_t  mpu_i2c_addr              = 0;
  float    kp_straight_enc           = 1.0f;
  float    kp_straight_gyro          = 6.0f;
  float    ki_straight_gyro          = 0.9f;
  float    kp_turn_enc               = 1.5f;
  float    kp_turn_gyro              = 8.0f;
  float    ki_turn_gyro              = 0.0f;
  float    heading_tolerance_deg     = 1.2f;
  float    stuck_rate_threshold_dps  = 5.0f;
  uint32_t stuck_timeout_ms          = 350;
  float    gyro_bias_alpha           = 0.08f;
  float    gyro_bias_threshold_dps   = 3.0f;
  uint32_t gyro_bias_settle_ms       = 400;
  float    yaw_integral_limit        = 35.0f;
  bool     use_gyro_for_straight     = true;   // Разрешать ли гироскопу корректировать прямой/обратный ход.
  float    duty_trim_forward         = 0.0f;   // Постоянный тримминг duty между бортами для движения вперёд (положительный -> больше тяга слева).
  float    duty_trim_backward        = 0.0f;   // Постоянный тримминг duty между бортами для движения назад (положительный -> больше тяга слева).
};

} // namespace Motion

