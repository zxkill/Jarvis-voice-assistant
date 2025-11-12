#include "motion_math.h"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

void test_equal_targets_yield_zero_error() {
  const auto result = Motion::compute_progress_error(120, 200, 120, 200);
  assert(result.scaled_error_ticks == 0);
  assert(std::fabs(result.progress_left - 0.6f) < 1e-6f);
  assert(std::fabs(result.progress_right - 0.6f) < 1e-6f);
}

void test_different_targets_equal_progress_is_zero() {
  const auto result = Motion::compute_progress_error(600, 1000, 750, 1250);
  assert(result.scaled_error_ticks == 0);
  assert(std::fabs(result.progress_left - 0.6f) < 1e-6f);
  assert(std::fabs(result.progress_right - 0.6f) < 1e-6f);
}

void test_right_wheel_ahead_produces_negative_error() {
  const auto result = Motion::compute_progress_error(500, 1000, 900, 1200);
  assert(result.scaled_error_ticks < 0);
  assert(std::fabs(result.progress_left - 0.5f) < 1e-6f);
  assert(std::fabs(result.progress_right - 0.75f) < 1e-6f);
}

void test_left_wheel_ahead_produces_positive_error() {
  const auto result = Motion::compute_progress_error(900, 1200, 400, 1000);
  assert(result.scaled_error_ticks > 0);
  assert(std::fabs(result.progress_left - 0.75f) < 1e-6f);
  assert(std::fabs(result.progress_right - 0.4f) < 1e-6f);
}

void test_zero_target_is_handled_gracefully() {
  const auto result = Motion::compute_progress_error(0, 0, 50, 500);
  assert(result.scaled_error_ticks == 0);
  assert(std::fabs(result.progress_left - 1.0f) < 1e-6f);
  assert(std::fabs(result.progress_right - 0.1f) < 1e-6f);
}

void test_turn_left_uses_symmetric_pwm() {
  const auto out = Motion::compute_turn_control(716.0f,
                                                1.5f,
                                                8.0f,
                                                0.0f,
                                                0,
                                                90.0f,
                                                0.0f,
                                                true,
                                                1023.0f);
  assert(std::fabs(out.duty_left - out.duty_right) < 1e-3f);
  assert(out.duty_left > 716.0f); // должны ускориться, чтобы догнать цель.
  assert(out.diff_component == 0.0f);
  assert(out.yaw_component > 0.0f); // положительная ошибка сигнализирует о недовороте влево.
}

void test_turn_right_sign_is_handled() {
  const auto out = Motion::compute_turn_control(716.0f,
                                                1.5f,
                                                8.0f,
                                                0.0f,
                                                0,
                                                -120.0f,
                                                0.0f,
                                                false,
                                                1023.0f);
  assert(std::fabs(out.duty_left - out.duty_right) < 1e-3f);
  assert(out.yaw_component > 0.0f); // отрицательная ошибка говорит, что ещё не докрутили вправо.
}

void test_turn_left_brakes_after_overshoot() {
  const auto out = Motion::compute_turn_control(500.0f,
                                                2.0f,
                                                6.0f,
                                                1.5f,
                                                0,
                                                -12.0f,
                                                -25.0f,
                                                true,
                                                1023.0f);
  assert(out.yaw_component < 0.0f);          // ошибка отрицательная -> замедляемся
  assert(out.duty_left < 500.0f);            // левый борт тормозим
  assert(out.duty_right < 500.0f);           // правый тоже замедляется
}

void test_turn_right_brakes_after_overshoot() {
  const auto out = Motion::compute_turn_control(520.0f,
                                                2.5f,
                                                5.0f,
                                                1.0f,
                                                0,
                                                15.0f,
                                                30.0f,
                                                false,
                                                1023.0f);
  assert(out.yaw_component < 0.0f);          // ошибка положительная -> тормозим
  assert(out.duty_left < 520.0f);
  assert(out.duty_right < 520.0f);
}

void test_turn_integral_is_symmetric_for_directions() {
  // Подробный комментарий: проверяем, что одинаковые по модулю ошибка и интеграл
  // создают равные корректирующие воздействия при поворотах в разные стороны.
  const auto left = Motion::compute_turn_control(450.0f,
                                                 0.0f,
                                                 4.0f,
                                                 1.5f,
                                                 0,
                                                 5.0f,
                                                 10.0f,
                                                 true,
                                                 1023.0f);
  const auto right = Motion::compute_turn_control(450.0f,
                                                  0.0f,
                                                  4.0f,
                                                  1.5f,
                                                  0,
                                                  -5.0f,
                                                  -10.0f,
                                                  false,
                                                  1023.0f);
  assert(left.yaw_component > 0.0f && right.yaw_component > 0.0f);
  assert(std::fabs(left.yaw_component - right.yaw_component) < 1e-3f);
  assert(std::fabs(left.duty_left - right.duty_left) < 1e-3f);
  assert(std::fabs(left.duty_right - right.duty_right) < 1e-3f);
}

void test_turn_with_encoder_error_balances_outputs() {
  const auto out = Motion::compute_turn_control(600.0f,
                                                2.0f,
                                                4.0f,
                                                0.0f,
                                                150,
                                                15.0f,
                                                0.0f,
                                                true,
                                                1023.0f);
  assert(out.diff_component > 0.0f);           // левое колесо ушло вперёд.
  assert(out.duty_left < out.duty_right);      // значит его нужно притормозить.
  assert(out.duty_left > 0.0f && out.duty_right > 0.0f);
}

void test_turn_scaling_preserves_ratio() {
  const auto out = Motion::compute_turn_control(900.0f,
                                                3.0f,
                                                6.0f,
                                                0.0f,
                                                -250,
                                                -80.0f,
                                                0.0f,
                                                false,
                                                800.0f);
  assert(out.scale_factor < 1.0f);
  assert(out.duty_left <= 800.0f && out.duty_right <= 800.0f);
  // Отношение должно сохраняться с точностью до маленькой погрешности округления.
  const float ratio = out.duty_left / out.duty_right;
  const float base_scaled = 0.5f * (out.duty_left + out.duty_right);
  const float raw_ratio = (base_scaled - out.diff_component) /
                          (base_scaled + out.diff_component);
  assert(std::fabs(ratio - raw_ratio) < 1e-3f);
}

} // namespace

int main() {
  std::cout << "Running motion math tests..." << std::endl;
  test_equal_targets_yield_zero_error();
  test_different_targets_equal_progress_is_zero();
  test_right_wheel_ahead_produces_negative_error();
  test_left_wheel_ahead_produces_positive_error();
  test_zero_target_is_handled_gracefully();
  test_turn_left_uses_symmetric_pwm();
  test_turn_right_sign_is_handled();
  test_turn_left_brakes_after_overshoot();
  test_turn_right_brakes_after_overshoot();
  test_turn_with_encoder_error_balances_outputs();
  test_turn_scaling_preserves_ratio();
  test_turn_integral_is_symmetric_for_directions();
  std::cout << "All motion math tests passed" << std::endl;
  return 0;
}

