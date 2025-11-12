#include "orientation.h"

#include <algorithm>
#include <cmath>

namespace {

float wrap_angle_deg(float angle) {
  // Подробный комментарий: используем std::fmodf, чтобы удерживать значение в
  // «компактном» диапазоне и исключить рост до тысяч градусов. После первичной
  // нормализации дополнительными проверками сдвигаем результат в [-180, 180).
  // Такой формат удобен для операторов, потому что знак сразу показывает
  // направление отклонения, а по величине легко понять, насколько робот ушёл.
  if (!std::isfinite(angle)) {
    return angle;
  }
  float wrapped = std::fmod(angle, 360.0f);
  if (wrapped < -180.0f) {
    wrapped += 360.0f;
  } else if (wrapped >= 180.0f) {
    wrapped -= 360.0f;
  }
  return wrapped;
}

} // namespace

namespace Orientation {

void YawIntegrator::reset(float yaw_deg, uint32_t timestamp_ms) {
  // Подробный комментарий: при сбросе мы явным образом задаём стартовый угол,
  // обнуляем накопленный поворот и помечаем, что данных для интеграции пока
  // нет. Это защищает от скачков после длительного простоя.
  has_reference_     = false;
  yaw_deg_unwrapped_ = yaw_deg;
  yaw_deg_wrapped_   = wrap_angle_deg(yaw_deg);
  last_rate_dps_     = 0.0f;
  last_raw_rate_dps_ = 0.0f;
  last_timestamp_ms_ = timestamp_ms;
  last_motion_ms_    = timestamp_ms;
}

void YawIntegrator::configure_bias(bool enabled, float alpha, float threshold_dps, uint32_t settle_ms) {
  // Комментарий: параметры фильтра задаются извне, чтобы можно было тонко
  // настраивать поведение под конкретный сенсор. Все значения валидируем,
  // чтобы избежать деления на ноль и других артефактов из-за некорректных
  // входных данных.
  bias_enabled_ = enabled;
  // std::clamp недоступен на некоторых платформах Arduino, поэтому выполняем
  // ручное ограничение коэффициента в диапазоне [0, 1], чтобы не зависеть от
  // конкретной версии стандартной библиотеки.
  if (alpha < 0.0f) {
    bias_alpha_ = 0.0f;
  } else if (alpha > 1.0f) {
    bias_alpha_ = 1.0f;
  } else {
    bias_alpha_ = alpha;
  }
  bias_threshold_dps_ = std::max(threshold_dps, 0.0f);
  bias_settle_ms_     = settle_ms;
}

void YawIntegrator::update(float gyro_z_dps, uint32_t timestamp_ms) {
  // Если обновление приходит «назад во времени», игнорируем его и просто
  // запоминаем скорость. Это защищает от некорректного времени в тестах.
  if (has_reference_ && timestamp_ms < last_timestamp_ms_) {
    last_raw_rate_dps_ = gyro_z_dps;
    last_rate_dps_     = gyro_z_dps - bias_dps_;
    return;
  }

  if (!has_reference_) {
    // Первое измерение: просто запоминаем время и скорость без интегрирования.
    has_reference_     = true;
    last_timestamp_ms_ = timestamp_ms;
    last_raw_rate_dps_ = gyro_z_dps;
    last_rate_dps_     = gyro_z_dps - bias_dps_;
    if (std::fabs(gyro_z_dps) > bias_threshold_dps_) {
      last_motion_ms_ = timestamp_ms;
    }
    return;
  }

  const uint32_t dt_ms = timestamp_ms - last_timestamp_ms_;
  const float dt_s     = static_cast<float>(dt_ms) / 1000.0f;

  last_raw_rate_dps_ = gyro_z_dps;

  // Оцениваем текущий уровень «шумности» измерения. По величине abs_rate мы
  // понимаем, можно ли считать, что робот стоит, и обновлять ли маркер движения.
  const float abs_rate = std::fabs(gyro_z_dps);
  // Если включена компенсация смещения и мы достаточно долго стоим, обновляем
  // bias экспоненциальным скользящим средним. Такой подход позволяет медленно
  // адаптироваться к температурному дрейфу, но не реагирует на реальное
  // движение.
  const bool quiet_enough = (abs_rate <= bias_threshold_dps_) &&
                            (timestamp_ms - last_motion_ms_ >= bias_settle_ms_);
  if (bias_enabled_ && quiet_enough) {
    const float delta = gyro_z_dps - bias_dps_;
    bias_dps_ += bias_alpha_ * delta;
  }

  const float corrected_rate = gyro_z_dps - bias_dps_;

  yaw_deg_unwrapped_ += corrected_rate * dt_s;
  yaw_deg_wrapped_    = wrap_angle_deg(yaw_deg_unwrapped_);
  last_rate_dps_  = corrected_rate;
  last_timestamp_ms_ = timestamp_ms;

  // Дополнительно следим за ситуацией, когда скорость явно выше порога — это
  // прямой индикатор движения, даже если bias ещё не успел подстроиться.
  if (abs_rate > bias_threshold_dps_) {
    last_motion_ms_ = timestamp_ms;
  }
}

bool YawIntegrator::is_stuck(uint32_t now_ms, float rate_threshold, uint32_t stagnation_ms) const {
  if (!has_reference_) {
    // Без валидных данных ничего утверждать нельзя — считаем, что всё ок.
    return false;
  }

  const float abs_rate = std::fabs(last_rate_dps_);
  if (abs_rate > rate_threshold) {
    // Есть вращение — явно не застряли.
    return false;
  }

  const uint32_t dt = (now_ms >= last_motion_ms_) ? (now_ms - last_motion_ms_) : 0u;
  return dt >= stagnation_ms;
}

} // namespace Orientation

