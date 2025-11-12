#pragma once

#include <stdint.h>

namespace Motion {

/// Класс-интегратор для накопления ошибки по курсу с защитой от виндапа.
/// Позволяет использовать PI-регулятор в цикле прямолинейного движения.
class YawIntegralAccumulator {
public:
  /// Инициализация и полный сброс состояния на указанное время.
  void reset(uint32_t now_ms) {
    // Подробный комментарий: при сбросе мы обнуляем накопленную ошибку и
    // синхронизируем «предыдущее» время с текущим, чтобы первый вызов update не
    // дал огромный скачок из-за старых данных.
    integral_     = 0.0f;
    last_ms_      = now_ms;
    initialized_  = true;
  }

  /// Конфигурация максимально допустимого интеграла, защищающая от виндапа.
  void set_limit(float limit_abs) {
    // Комментарий: лимит хранится по модулю, чтобы избежать отрицательных
    // значений и упростить последующую клампацию.
    limit_abs_ = (limit_abs < 0.0f) ? -limit_abs : limit_abs;
  }

  /// Обновление интегральной ошибки и возвращение актуального значения.
  float update(float yaw_err_deg, uint32_t now_ms) {
    if (!initialized_) {
      // Если забыли вызвать reset, делаем это автоматически, чтобы не получить
      // некорректный dt. Это особенно полезно в тестах.
      reset(now_ms);
    }

    const uint32_t dt_ms = (now_ms >= last_ms_) ? (now_ms - last_ms_) : 0u;
    last_ms_             = now_ms;

    const float dt_s = static_cast<float>(dt_ms) / 1000.0f;
    integral_ += yaw_err_deg * dt_s;

    if (integral_ > limit_abs_) {
      integral_ = limit_abs_;
    } else if (integral_ < -limit_abs_) {
      integral_ = -limit_abs_;
    }

    return integral_;
  }

  /// Текущее значение интегральной ошибки (°*с).
  float value() const { return integral_; }

private:
  float    integral_    = 0.0f;   ///< Накопленная ошибка в градусах*секундах
  float    limit_abs_   = 35.0f;  ///< Предельное по модулю значение интеграла
  uint32_t last_ms_     = 0;      ///< Время последнего обновления
  bool     initialized_ = false;  ///< Были ли уже реальные данные
};

} // namespace Motion

