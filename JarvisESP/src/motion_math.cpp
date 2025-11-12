#include "motion_math.h"

#include <cmath>

namespace Motion {
namespace {

/// Вспомогательная функция: безопасно делим тики на цель, чтобы получить прогресс.
static float compute_progress_ratio(long ticks, long target) {
  if (target == 0) {
    // Подробный комментарий: если цель нулевая (команда с нулевым расстоянием),
    // считаем прогресс равным единице. Так корректор не будет пытаться
    // «догонять» несуществующую цель и не породит NaN из-за деления на ноль.
    return 1.0f;
  }
  return static_cast<float>(ticks) / static_cast<float>(target);
}

/// Ручной вариант функции clamp: Arduino-платформы не всегда предоставляют
/// std::clamp, поэтому ограничиваем значение самостоятельно.
static float clamp_value(float value, float min_value, float max_value) {
  if (value < min_value) {
    return min_value;
  }
  if (value > max_value) {
    return max_value;
  }
  return value;
}

} // namespace

ProgressError compute_progress_error(long ticks_left,
                                     long target_left,
                                     long ticks_right,
                                     long target_right) {
  ProgressError result{};

  result.progress_left  = compute_progress_ratio(ticks_left, target_left);
  result.progress_right = compute_progress_ratio(ticks_right, target_right);

  if (target_left == 0 || target_right == 0) {
    // Если одна из целей равна нулю, считаем, что дополнительная коррекция не
    // требуется. Такая ситуация возможна, например, когда одно колесо должно
    // стоять (локальный разворот вокруг опоры). Возвращаем нулевую ошибку, но
    // сохраняем рассчитанные прогрессы для логов.
    result.scaled_error_ticks = 0;
    return result;
  }

  // Нормированная разница прогресса: положительное значение означает, что левое
  // колесо ушло вперёд, отрицательное — правое. При равных прогрессах получаем 0.
  const float progress_diff = result.progress_left - result.progress_right;

  // Масштабируем ошибку обратно в условные «тики». В качестве масштаба берём
  // среднюю величину цели, чтобы сохранить ожидаемый порядок значений. При очень
  // маленьких целях страхуемся единицей, чтобы не делить на ноль и не терять
  // чувствительность на коротких проездах.
  const float avg_target = 0.5f * (std::fabs(static_cast<float>(target_left)) +
                                   std::fabs(static_cast<float>(target_right)));
  const float scale = (avg_target > 1.0f) ? avg_target : 1.0f;

  result.scaled_error_ticks = static_cast<long>(std::lround(progress_diff * scale));
  return result;
}

TurnControlOutput compute_turn_control(float duty_base,
                                       float kp_enc,
                                       float kp_gyro,
                                       float ki_gyro,
                                       long  err_enc,
                                       float yaw_err_deg,
                                       float yaw_integral,
                                       bool  turning_left,
                                       float pwm_max) {
  TurnControlOutput out{};

  // Подробный комментарий: для поворота вокруг своей оси нужно, чтобы обе гусеницы
  // (колёса) меняли скорость синфазно. Поэтому мы раскладываем задачу на две
  // составляющие: yaw_component отвечает за «ускорение» или торможение поворота в
  // целом, а diff_component — за баланс между левым и правым колесом.

  constexpr float LIMIT = 220.0f; // тот же диапазон, что используется в прошивке для duty-коррекций.

  const float enc_raw = kp_enc * static_cast<float>(err_enc);
  // Ограничиваем дифференциальную часть, чтобы избежать экстремальных перекосов.
  const float enc_limited = clamp_value(enc_raw, -LIMIT, LIMIT);

  // Компенсируем знак yaw в зависимости от направления поворота. Ошибка передаётся
  // в формате «цель минус текущее», поэтому при повороте влево положительное
  // значение означает, что мы ещё не докрутили и нужно ускоряться. Для правого
  // поворота инвертируем знак, чтобы логика осталась симметричной.
  const float yaw_err_signed = turning_left ? yaw_err_deg : -yaw_err_deg;
  const float yaw_int_signed = turning_left ? yaw_integral : -yaw_integral;

  const float yaw_raw = kp_gyro * yaw_err_signed + ki_gyro * yaw_int_signed;
  const float yaw_limited = clamp_value(yaw_raw, -LIMIT, LIMIT);

  const float base_with_yaw = duty_base + yaw_limited;

  float duty_left  = base_with_yaw - enc_limited;
  float duty_right = base_with_yaw + enc_limited;

  if (duty_left < 0.0f)  duty_left  = 0.0f;
  if (duty_right < 0.0f) duty_right = 0.0f;

  // Находим максимальное значение вручную: это надёжно на всех компиляторах,
  // включая Arduino, где нет перегрузки std::max для initializer_list.
  float max_pwm = duty_left;
  if (duty_right > max_pwm) {
    max_pwm = duty_right;
  }
  if (max_pwm < 0.0f) {
    max_pwm = 0.0f;
  }

  float scale = 1.0f;
  if (max_pwm > pwm_max && max_pwm > 0.0f) {
    scale = pwm_max / max_pwm;
    duty_left  *= scale;
    duty_right *= scale;
  }

  out.duty_left      = duty_left;
  out.duty_right     = duty_right;
  out.yaw_component  = yaw_limited * scale;
  out.diff_component = enc_limited * scale;
  out.scale_factor   = scale;
  return out;
}

StraightCorrectionOutput compute_straight_correction(float kp_enc,
                                                     float kp_gyro,
                                                     float ki_gyro,
                                                     long  err_enc,
                                                     float yaw_err_deg,
                                                     float yaw_integral,
                                                     bool  allow_gyro,
                                                     float correction_limit) {
  StraightCorrectionOutput out{};

  // Детальный комментарий: сначала вычисляем отдельные составляющие, чтобы
  // позже можно было вывести их в лог и понять, какую долю даёт энкодер, а
  // какую — гироскоп. Это особенно полезно, когда мы временно отключаем yaw
  // для прямого хода и хотим убедиться, что вклад действительно равен нулю.
  out.encoder_term = kp_enc * static_cast<float>(err_enc);

  if (allow_gyro) {
    out.yaw_term      = kp_gyro * yaw_err_deg;
    out.integral_term = ki_gyro * yaw_integral;
    out.gyro_contribution_used = (std::fabs(out.yaw_term) > 1e-6f) ||
                                 (std::fabs(out.integral_term) > 1e-6f);
  } else {
    out.yaw_term = 0.0f;
    out.integral_term = 0.0f;
    out.gyro_contribution_used = false;
  }

  const float combined = out.encoder_term + out.yaw_term + out.integral_term;
  out.combined_raw = combined;

  // Ограничиваем общее значение, чтобы не выйти за допустимый диапазон ШИМ и не
  // породить отрицательные duty у колеса, которое должно разгоняться.
  out.combined_limited = clamp_value(combined, -correction_limit, correction_limit);

  out.duty_delta = static_cast<int>(std::lround(out.combined_limited));
  return out;
}

void EncoderProgressMonitor::reset(uint32_t now_ms) {
  // Подробный комментарий: сброс выполняется перед стартом манёвра, поэтому
  // обнуляем оба счётчика и фиксируем текущее время как последнюю «живую» точку.
  last_left_ticks = 0;
  last_right_ticks = 0;
  last_progress_ms = now_ms;
}

bool EncoderProgressMonitor::update(long left_ticks, long right_ticks, uint32_t now_ms) {
  // Если значения тиков изменились хотя бы у одного колеса, считаем, что
  // прогресс есть, и переносим временную метку. Такой подход корректно работает
  // как при прямом движении, так и при езде назад: достаточно сравнить сырые
  // значения, не беря модуль.
  if (left_ticks != last_left_ticks || right_ticks != last_right_ticks) {
    last_left_ticks = left_ticks;
    last_right_ticks = right_ticks;
    last_progress_ms = now_ms;
    return true;
  }
  return false;
}

bool EncoderProgressMonitor::is_timed_out(uint32_t now_ms, uint32_t timeout_ms) const {
  // Страхуемся от переполнения millis: сравниваем через беззнаковую арифметику.
  const uint32_t elapsed = now_ms - last_progress_ms;
  return elapsed > timeout_ms;
}

} // namespace Motion

