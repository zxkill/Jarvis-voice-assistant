// ServoController.h
#pragma once
#include <Arduino.h>
#include "Logger.h"

class ServoController {
public:
  struct Tuning {
    // Пропорциональный коэффициент: сколько градусов поворачивать
    // серву за каждый пиксель ошибки. Значения близки к углу обзора
    // камеры (≈38° по X и ≈62° по Y) делённому на разрешение кадра.
    float kpYawDegPerPx   = 0.06f;  // чувствительность по оси Yaw
    float kpPitchDegPerPx = 0.10f;  // чувствительность по оси Pitch

    // Плавность подхода к целевому углу: какая доля оставшегося
    // смещения будет выполнена за один цикл (0…1).
    float smoothYaw       = 0.25f;  // сглаживание по Yaw
    float smoothPitch     = 0.25f;  // и по Pitch

    // Мёртвая зона в пикселях: если лицо близко к центру, ошибки
    // меньше deadzonePx игнорируются, предотвращая дрожание.
    float deadzoneYawPx   = 10.0f;  // горизонтальная зона покоя
    float deadzonePitchPx = 10.0f;  // вертикальная зона покоя

    // Инверсия осей (если серва физически стоит наоборот)
    bool  invertYaw       = true;    // ← частая причина «убегания» — меняем знак
    bool  invertPitch     = false;

    // Клипы по углам, чтобы не ломать рог
    float yawMinDeg       = -70.0f;
    float yawMaxDeg       =  +70.0f;
    float pitchMinDeg     = -65.0f;
    float pitchMaxDeg     =  +65.0f;

    // Трим по центру, если физически нейтраль не ровно по центру
    float trimYawDeg      = 0.0f;
    float trimPitchDeg    = 0.0f;

    // Диапазон импульса сервы (если твои сервы любят 500–2400 мкс — верни)
    uint16_t minPulseUs   = 1000;    // стандартный безопасный минимум
    uint16_t maxPulseUs   = 2000;    // и максимум
  };

public:
  void begin();                                       // инициализация PWM
  void setAngles(float yawDeg, float pitchDeg);       // абсолютные углы −90…+90 (для ручной калибровки)
  void center();                                      // перейти в нейтраль (с учётом тримов)

  // Новый «правильный» способ: подать ошибки dx/dy в пикселях и (опционально) dt мс
  void updateFromError(float dx_px, float dy_px, uint32_t dt_ms = 0);

  // Настройки
  void setTuning(const Tuning& t);
  const Tuning& tuning() const { return cfg; }

  // Текущее состояние (с учётом тримов и клипов)
  float currentYawDeg() const   { return curYawDeg;   }
  float currentPitchDeg() const { return curPitchDeg; }

private:
  // Аппаратные пины
  static constexpr uint8_t pinYaw   = 17;    // левый сервопривод
  static constexpr uint8_t pinPitch = 26;    // правый (Grove B Y-wire)

  // LEDC-настройки (каналы ≠ 0, чтобы не конфликтовать со спикером)
  static constexpr uint8_t  chYaw    = 1;     // канал 1
  static constexpr uint8_t  chPitch  = 2;     // канал 2
  static constexpr uint32_t pwmFreq  = 50;    // 50 Гц – стандарт для RC-серв
  static constexpr uint8_t  pwmRes   = 16;    // 16-битный таймер
  static constexpr uint32_t dutyMax  = (1 << pwmRes) - 1;  // 65535
  static constexpr uint32_t periodUs = 1000000UL / pwmFreq; // 20 000 мкс

  // Перевод «длина импульса → duty»
  static inline uint32_t pulseToDuty(uint32_t us) {
    return (uint64_t)us * dutyMax / periodUs;
  }

  // Хелперы
  void _applyAngles(float yawDeg, float pitchDeg);
  uint32_t _angleToPulseUs(float angleDeg, bool isYaw) const;

private:
  Tuning cfg{};
  float  curYawDeg   = 0.0f;
  float  curPitchDeg = 0.0f;

  // Для плавного движения храним текущее состояние серв (угол) —
  // дополнительных буферов не требуется.
};
