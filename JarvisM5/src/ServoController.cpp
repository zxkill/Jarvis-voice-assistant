// ServoController.cpp
#include "ServoController.h"

// ——— Инициализация PWM ———————————————————————————————————
void ServoController::begin() {
  ledcSetup(chYaw, pwmFreq, pwmRes);
  ledcAttachPin(pinYaw, chYaw);
  ledcSetup(chPitch, pwmFreq, pwmRes);
  ledcAttachPin(pinPitch, chPitch);

  // Запускаем с нейтрали
  center();

  Logger::log(LogLevel::INFO,
              "ServoController: LEDC ch=%d/%d @%lu Hz, res=%d bit",
              chYaw, chPitch, (unsigned long)pwmFreq, pwmRes);

  Logger::log(LogLevel::INFO,
              "ServoController: pulses %u..%u us; yaw clip [%.1f..%.1f], pitch clip [%.1f..%.1f]; inv(Y:%d,P:%d) gain yaw=%.3f pitch=%.3f smooth yaw=%.2f pitch=%.2f deadzone yaw=%.1fpx pitch=%.1fpx",
              cfg.minPulseUs, cfg.maxPulseUs,
              cfg.yawMinDeg, cfg.yawMaxDeg, cfg.pitchMinDeg, cfg.pitchMaxDeg,
              (int)cfg.invertYaw, (int)cfg.invertPitch,
              cfg.kpYawDegPerPx, cfg.kpPitchDegPerPx,
              cfg.smoothYaw, cfg.smoothPitch, cfg.deadzoneYawPx, cfg.deadzonePitchPx);
}

// ——— Публичный API —————————————————————————————————
void ServoController::setTuning(const Tuning& t) {
  cfg = t;
  Logger::log(LogLevel::INFO,
              "Servo Tuning set: gain yaw=%.3f pitch=%.3f smooth yaw=%.2f pitch=%.2f deadzone yaw=%.1fpx pitch=%.1fpx inv(Y:%d,P:%d) yaw[%.1f..%.1f] pitch[%.1f..%.1f] trim(%.1f/%.1f) pulses %u..%u",
              cfg.kpYawDegPerPx, cfg.kpPitchDegPerPx,
              cfg.smoothYaw, cfg.smoothPitch, cfg.deadzoneYawPx, cfg.deadzonePitchPx,
              (int)cfg.invertYaw, (int)cfg.invertPitch,
              cfg.yawMinDeg, cfg.yawMaxDeg, cfg.pitchMinDeg, cfg.pitchMaxDeg,
              cfg.trimYawDeg, cfg.trimPitchDeg, cfg.minPulseUs, cfg.maxPulseUs);
}

void ServoController::center() {
  curYawDeg   = 0.0f;
  curPitchDeg = 0.0f;
  _applyAngles(curYawDeg, curPitchDeg);
  Logger::log(LogLevel::INFO,
              "Servo center: yaw=%.1f pitch=%.1f (with trims %.1f/%.1f)",
              curYawDeg, curPitchDeg, cfg.trimYawDeg, cfg.trimPitchDeg);
}

void ServoController::setAngles(float yawDeg, float pitchDeg) {
  // Абсолютная установка (для калибровки)
  curYawDeg   = constrain(yawDeg,   cfg.yawMinDeg,   cfg.yawMaxDeg);
  curPitchDeg = constrain(pitchDeg, cfg.pitchMinDeg, cfg.pitchMaxDeg);
  _applyAngles(curYawDeg, curPitchDeg);

  Logger::log(LogLevel::DEBUG,
              "[ABS] yaw=%.1f° pitch=%.1f°  → pulses Y=%luus P=%luus",
              curYawDeg, curPitchDeg,
              (unsigned long)_angleToPulseUs(curYawDeg, true),
              (unsigned long)_angleToPulseUs(curPitchDeg, false));
}

void ServoController::updateFromError(float dx_px, float dy_px, uint32_t /*dt_ms*/) {
  // 1) Ошибка по каждой оси с учётом направления сервы
  float ex = cfg.invertYaw   ? -dx_px : dx_px;
  float ey = cfg.invertPitch ? -dy_px : dy_px;

  // 2) Гашение дрожания: в пределах мёртвой зоны не двигаемся
  if (fabsf(ex) <= cfg.deadzoneYawPx)   ex = 0.0f;
  if (fabsf(ey) <= cfg.deadzonePitchPx) ey = 0.0f;

  // 3) Целевые углы с учётом пропорционального коэффициента
  float targetYaw   = constrain(curYawDeg   + ex * cfg.kpYawDegPerPx,
                                cfg.yawMinDeg,   cfg.yawMaxDeg);
  float targetPitch = constrain(curPitchDeg + ey * cfg.kpPitchDegPerPx,
                                cfg.pitchMinDeg, cfg.pitchMaxDeg);

  // 4) Плавное приближение к цели
  curYawDeg   += (targetYaw   - curYawDeg)   * cfg.smoothYaw;
  curPitchDeg += (targetPitch - curPitchDeg) * cfg.smoothPitch;

  _applyAngles(curYawDeg, curPitchDeg);

  // 5) Логируем
  Logger::log(LogLevel::DEBUG,
              "[SMTH] err(px)=(%.1f,%.1f) target=(%.2f,%.2f) → angle=(%.1f,%.1f)",
              dx_px, dy_px, targetYaw, targetPitch, curYawDeg, curPitchDeg);
}

// ——— Внутренние хелперы —————————————————————————————————
void ServoController::_applyAngles(float yawDeg, float pitchDeg) {
  // Добавляем трим (смещение нуля)
  float yawWithTrim   = yawDeg   + cfg.trimYawDeg;
  float pitchWithTrim = pitchDeg + cfg.trimPitchDeg;

  // Перевод в PWM
  uint32_t pulseYawUs   = _angleToPulseUs(yawWithTrim,  true);
  uint32_t pulsePitchUs = _angleToPulseUs(pitchWithTrim, false);

  ledcWrite(chYaw,   pulseToDuty(pulseYawUs));
  ledcWrite(chPitch, pulseToDuty(pulsePitchUs));
}

uint32_t ServoController::_angleToPulseUs(float angleDeg, bool isYaw) const {
  // Угол −90…+90 переводим в 0…180
  float a180 = constrain(angleDeg + 90.0f, 0.0f, 180.0f);

  // Линейная интерполяция в пределах настроек
  uint32_t pulse = cfg.minPulseUs +
                   (uint32_t)((cfg.maxPulseUs - cfg.minPulseUs) * (a180 / 180.0f));

  // Защитные клипы (на случай корявых настроек)
  pulse = constrain(pulse, (uint32_t)500, (uint32_t)2500);

  return pulse;
}

