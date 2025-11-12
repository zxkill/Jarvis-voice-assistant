#include "motion_math.h"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

void test_encoder_only_mode_ignores_gyro() {
  // Подробный комментарий: имитируем движение прямо, где yaw отключён. Даже при
  // ненулевых ошибках курса результат должен зависеть исключительно от энкодера.
  const auto out = Motion::compute_straight_correction(2.5f,
                                                       7.0f,
                                                       0.8f,
                                                       40,
                                                       15.0f,
                                                       3.0f,
                                                       false,
                                                       220.0f);
  assert(std::fabs(out.encoder_term - 100.0f) < 1e-6f);
  assert(std::fabs(out.yaw_term) < 1e-6f);
  assert(std::fabs(out.integral_term) < 1e-6f);
  assert(std::fabs(out.combined_raw - 100.0f) < 1e-6f);
  assert(out.duty_delta == 100);
  assert(!out.gyro_contribution_used);
}

void test_gyro_enabled_combines_all_terms() {
  const auto out = Motion::compute_straight_correction(1.8f,
                                                       4.0f,
                                                       0.5f,
                                                       -30,
                                                       -6.0f,
                                                       2.0f,
                                                       true,
                                                       220.0f);
  // Энкодер стремится увеличить левое колесо (отрицательная ошибка -> отрицательный вклад).
  assert(std::fabs(out.encoder_term + 54.0f) < 1e-6f);
  // Гироскоп и интеграл должны дать положительный вклад, компенсируя уваливание.
  assert(std::fabs(out.yaw_term - (-24.0f)) < 1e-6f);
  assert(std::fabs(out.integral_term - 1.0f) < 1e-6f);
  // Итоговая коррекция ограничена диапазоном, поэтому дельта отрицательная.
  assert(out.duty_delta == static_cast<int>(std::lround(out.combined_limited)));
  assert(out.gyro_contribution_used);
}

void test_correction_is_clamped() {
  const auto out = Motion::compute_straight_correction(6.0f,
                                                       5.0f,
                                                       1.0f,
                                                       80,
                                                       40.0f,
                                                       10.0f,
                                                       true,
                                                       50.0f);
  // Без клампа сумма была бы намного больше 50.
  assert(out.combined_raw > 100.0f);
  assert(std::fabs(out.combined_limited - 50.0f) < 1e-6f);
  assert(out.duty_delta == 50);
}

} // namespace

int main() {
  std::cout << "Running straight correction tests..." << std::endl;
  test_encoder_only_mode_ignores_gyro();
  test_gyro_enabled_combines_all_terms();
  test_correction_is_clamped();
  std::cout << "All straight correction tests passed" << std::endl;
  return 0;
}

