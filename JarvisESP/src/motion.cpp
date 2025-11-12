#include "motion.h"
#include "sensors.h"
#include "heading_control.h"
#include "motion_math.h"

#include <cmath>

namespace Motion {

static Params P;
static int CH_L_FWD = 0, CH_L_BACK = 1, CH_R_FWD = 2, CH_R_BACK = 3;
static int PWM_MAX  = 1023;
static bool gPwmConfigured = false; ///< Флаг, что PWM-каналы уже инициализированы хотя бы раз.

// --- Состояние гироскопа ---
static Orientation::YawIntegrator gYaw;     ///< Интегратор курса, ответственный за вычисление yaw.
static bool gGyroConfigured = false;        ///< Настроен ли гироскоп и разрешена ли коррекция курса.
static uint32_t gLastGyroReadMs = 0;        ///< Метка времени последнего чтения MPU6050.
static uint32_t gLastProgressMs = 0;        ///< Метка времени последнего прогресса по энкодерам.

/**
 * \brief Проверяет, совпадают ли аппаратно-зависимые поля параметров.
 *
 * Этот вспомогательный метод вынесен отдельно, чтобы как инициализация,
 * так и «тёплое» обновление параметров могли одинаково определять, требуется
 * ли повторная настройка PWM-каналов. Мы сравниваем только те значения,
 * которые реально влияют на конфигурацию таймеров и пинов.
 */
static bool hardware_fields_equal(const Params& lhs, const Params& rhs) {
  return lhs.pwm_freq_hz      == rhs.pwm_freq_hz &&
         lhs.pwm_res_bits     == rhs.pwm_res_bits &&
         lhs.pin_left_pwm_fwd  == rhs.pin_left_pwm_fwd &&
         lhs.pin_left_pwm_back == rhs.pin_left_pwm_back &&
         lhs.pin_right_pwm_fwd == rhs.pin_right_pwm_fwd &&
         lhs.pin_right_pwm_back== rhs.pin_right_pwm_back &&
         lhs.use_left_enable   == rhs.use_left_enable &&
         lhs.pin_left_en_a     == rhs.pin_left_en_a &&
         lhs.pin_left_en_b     == rhs.pin_left_en_b;
}

/**
 * \brief Печатает сводку текущих настроек движения в Serial-монитор.
 *
 * Отдельная функция позволяет переиспользовать формат сообщений как во время
 * холодного старта, так и при обновлении параметров «на лету», избегая
 * дублирования строковых констант и сохраняя единый стиль логирования.
 */
static void log_configuration_summary(const Params& params, const char* phaseLabel) {
  Serial.printf("[MOTION] %s: freq=%dHz res=%dbit PWM_MAX=%d\n",
                phaseLabel,
                params.pwm_freq_hz,
                params.pwm_res_bits,
                PWM_MAX);
  Serial.printf("[MOTION] %s: pins L(fwd=%d, back=%d)%s  R(fwd=%d, back=%d)\n",
                phaseLabel,
                params.pin_left_pwm_fwd,
                params.pin_left_pwm_back,
                params.use_left_enable ? " EN=ON" : " EN=OFF",
                params.pin_right_pwm_fwd,
                params.pin_right_pwm_back);
  Serial.printf("[MOTION] %s: geom: D=%.1fmm  base=%.1fmm  TPR(L/R)=%ld/%ld  ENC-left=%s\n",
                phaseLabel,
                params.wheel_d_mm,
                params.wheel_base_mm,
                params.tpr_left,
                params.tpr_right,
                params.enc_left_is_enc1 ? "ENC1" : "ENC2");
  Serial.printf("[MOTION] %s: trims: forward=%.2f backward=%.2f\n",
                phaseLabel,
                params.duty_trim_forward,
                params.duty_trim_backward);
  if (params.enable_gyro_feedback) {
    Serial.printf("[MOTION] %s: gyro: addr=0x%02X kpStraight(enc=%.2f gyro=%.2f) kpTurn(enc=%.2f gyro=%.2f) tol=%.2f° straightYaw=%s\n",
                  phaseLabel,
                  params.mpu_i2c_addr,
                  params.kp_straight_enc,
                  params.kp_straight_gyro,
                  params.kp_turn_enc,
                  params.kp_turn_gyro,
                  params.heading_tolerance_deg,
                  params.use_gyro_for_straight ? "ON" : "OFF");
    Serial.printf("[MOTION] %s: gyro bias filter: alpha=%.3f threshold=%.2f°/s settle=%ums\n",
                  phaseLabel,
                  params.gyro_bias_alpha,
                  params.gyro_bias_threshold_dps,
                  params.gyro_bias_settle_ms);
  } else {
    Serial.printf("[MOTION] %s: gyro: feedback disabled\n", phaseLabel);
  }
}

/**
 * \brief Обновляет кеш параметров и настройки фильтра курса.
 *
 * После успешной аппаратной инициализации, либо после «тёплого» обновления
 * коэффициентов мы вызываем этот метод, чтобы синхронно обновить глобальные
 * переменные, таймауты и вывести диагностическую информацию.
 *
 * @param params        Новый набор параметров.
 * @param resetHeading  Нужно ли сбрасывать интегратор курса (true при
 *                      переинициализации PWM).
 * @param phaseLabel    Текстовый маркер, попадающий в лог, чтобы проще было
 *                      отделять сообщения старта от Runtime-обновлений.
 */
static void apply_cached_params(const Params& params,
                                bool resetHeading,
                                const char* phaseLabel) {
  P = params;
  PWM_MAX = (1 << P.pwm_res_bits) - 1;

  const uint32_t nowMs = millis();
  gGyroConfigured = P.enable_gyro_feedback && P.mpu_i2c_addr != 0;
  gYaw.configure_bias(gGyroConfigured,
                      P.gyro_bias_alpha,
                      P.gyro_bias_threshold_dps,
                      P.gyro_bias_settle_ms);
  if (resetHeading && gGyroConfigured) {
    gYaw.reset(0.0f, nowMs);
    Serial.println(F("[MOTION] init: yaw-интегратор сброшен после аппаратной перенастройки"));
  }

  gLastGyroReadMs = nowMs;
  gLastProgressMs = nowMs;

  log_configuration_summary(P, phaseLabel);
}

// --- Управление отменой манёвров ---
static volatile bool gAbortRequested = false; ///< Флаг мягкой отмены текущего манёвра.

static const float KP_STRAIGHT = 3.0f;   // базовый коэффициент для старого поведения
static const int   LOG_EVERY_MS = 50;    // как часто логировать в цикле

static const int   CORR_CLAMP     = 220;  // ограничим коррекцию по duty
static const int   MIN_DUTY_MOVE  = 100;  // Мёртвая зона моторов: снижена для предотвращения насыщения duty.
static inline int apply_deadband(int duty) {
  return (duty > 0 && duty < MIN_DUTY_MOVE) ? MIN_DUTY_MOVE : duty;
}

/**
 * \brief Применяет постоянный тримминг duty для конкретного борта.
 *
 * При калибровке может оказаться, что один мотор стабильно быстрее другого в
 * определённом направлении. Чтобы избежать постоянного увода и не нагружать
 * регулятор лишней работой, мы разрешаем задать «трими» — небольшие смещения
 * duty относительно базового значения. Положительный trim усиливает левый борт,
 * отрицательный — правый.
 *
 * @param baseDuty   Базовое значение ШИМ перед коррекциями.
 * @param trimValue  Конфигурируемый тримминг (в условных единицах duty).
 * @param leftWheel  true, если обрабатываем левый мотор.
 * @return Придвинутый duty с учётом тримминга (округляется до ближайшего int).
 */
static inline int apply_directional_trim(int baseDuty, float trimValue, bool leftWheel) {
  const float signedTrim = leftWheel ? trimValue : -trimValue;
  const float adjusted = static_cast<float>(baseDuty) + signedTrim;
  return static_cast<int>(std::lround(adjusted));
}

/**
 * \brief Приводит угол к диапазону [-180°, 180°).
 *
 * Функция нужна для человеко-читаемых логов: регуляторы оперируют «развёрнутым»
 * углом, который может расти на тысячи градусов при многократных оборотах.
 * Чтобы оператору было легче считывать текущий курс, нормализуем значение,
 * не влияя на расчёт ошибок.
 */
static float wrap_heading_deg(float angle) {
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

// --- Состояние гироскопа ---
/// Локальный хелпер для обрезки duty.
static inline int clamp_pwm(int duty) {
  if (duty < 0) duty = 0;
  if (duty > PWM_MAX) duty = PWM_MAX;
  return duty;
}

/// Расчёт корректирующего воздействия из ошибок по энкодерам и гироскопу.
// Вспомогательно: какие энкодеры считаем лев/прав
static inline EncId enc_left()  { return P.enc_left_is_enc1 ? ENC1 : ENC2; }
static inline EncId enc_right() { return P.enc_left_is_enc1 ? ENC2 : ENC1; }

// === Низкоуровневое управление (с логированием duty) ===
void left_forward (int duty)   { duty = constrain(duty, 0, PWM_MAX); if (P.use_left_enable){digitalWrite(P.pin_left_en_a,HIGH);digitalWrite(P.pin_left_en_b,HIGH);} ledcWrite(CH_L_FWD, duty); ledcWrite(CH_L_BACK, 0); }
void left_backward(int duty)   { duty = constrain(duty, 0, PWM_MAX); if (P.use_left_enable){digitalWrite(P.pin_left_en_a,HIGH);digitalWrite(P.pin_left_en_b,HIGH);} ledcWrite(CH_L_FWD, 0);    ledcWrite(CH_L_BACK, duty); }
void left_coast   ()           { ledcWrite(CH_L_FWD, 0); ledcWrite(CH_L_BACK, 0); }

void right_forward (int duty)  { duty = constrain(duty, 0, PWM_MAX); ledcWrite(CH_R_FWD, duty); ledcWrite(CH_R_BACK, 0); }
void right_backward(int duty)  { duty = constrain(duty, 0, PWM_MAX); ledcWrite(CH_R_FWD, 0);    ledcWrite(CH_R_BACK, duty); }
void right_coast   ()          { ledcWrite(CH_R_FWD, 0); ledcWrite(CH_R_BACK, 0); }

void stop_all() { left_coast(); right_coast(); }

// === Диагностика энкодеров ===
void get_stats(long& dt_left, long& dt_right, long& peek_left, long& peek_right) {
  dt_left    = enc_get_and_clear(enc_left());
  dt_right   = enc_get_and_clear(enc_right());
  peek_left  = enc_peek(enc_left());
  peek_right = enc_peek(enc_right());
}

// === Хелперы конвертации ===
static inline long meters_to_ticks_left (float m) {
  float C_mm = 3.1415926f * P.wheel_d_mm;
  return (long)((m * 1000.0f) * (float)P.tpr_left  / C_mm);
}
static inline long meters_to_ticks_right(float m) {
  float C_mm = 3.1415926f * P.wheel_d_mm;
  return (long)((m * 1000.0f) * (float)P.tpr_right / C_mm);
}

// === Блокирующие движения по одометрии ===
void forward_m(float meters, int dutyBase) {
  // === Расчёт целевых тиков ===
  // отдельные TPR для левого/правого
  const float C_mm   = PI * P.wheel_d_mm;          // длина окружности колеса
  const float M_mm   = meters * 1000.0f;
  const long  tL     = (long)roundf((M_mm / C_mm) * (float)P.tpr_left);
  const long  tR     = (long)roundf((M_mm / C_mm) * (float)P.tpr_right);

  // === Подготовка ===
  enc_get_and_clear(enc_left());
  enc_get_and_clear(enc_right());

  Serial.printf("[MOTION] FWD %.3fm -> L:%ld ticks, R:%ld ticks, dutyBase=%d\n", meters, tL, tR, dutyBase);

  // Подробный комментарий: при старте движения мы сбрасываем «таймер прогресса»
  // на текущее время. Иначе защита от застревания может преждевременно
  // сработать сразу после запуска, если робот некоторое время стоял без
  // движения. Так мы даём колёсам честный интервал на раскрутку.
  const uint32_t startNow = millis();
  gLastProgressMs = startNow;
  Serial.printf("[MOTION] stall watchdog armed at %lu ms\n", static_cast<unsigned long>(startNow));

  // Подробный комментарий: при отключённом гироскопе нам нужен независимый
  // контроль прогресса по энкодерам, чтобы не потерять защиту от застревания.
  Motion::EncoderProgressMonitor progressMonitor;
  progressMonitor.reset(startNow);

  const float trimForward = P.duty_trim_forward;
  int dutyL = apply_directional_trim(dutyBase, trimForward, true);
  int dutyR = apply_directional_trim(dutyBase, trimForward, false);
  dutyL = (dutyL > 0) ? apply_deadband(clamp_pwm(dutyL)) : 0;
  dutyR = (dutyR > 0) ? apply_deadband(clamp_pwm(dutyR)) : 0;
  bool abortedByStall = false;     // Флаг, который покажем после цикла

  // Подробный комментарий: временно отключаем вклад гироскопа при прямолинейном
  // ходе, чтобы откалибровать энкодеры. Однако сам объект интегратора оставляем
  // на готове, чтобы в будущем можно было легко вернуть поведение обратно.
  const bool gyroConfigured = P.enable_gyro_feedback && P.mpu_i2c_addr != 0;
  const bool allowYawForStraight = P.use_gyro_for_straight; // Читаем пользовательский флаг на использование yaw.
  const bool useGyroForStraight = gyroConfigured && allowYawForStraight;
  Motion::YawIntegralAccumulator yawPi;
  if (useGyroForStraight) {
    yawPi.set_limit(P.yaw_integral_limit);
    yawPi.reset(startNow);
  }

  if (gyroConfigured) {
    if (useGyroForStraight) {
      Serial.println("[MOTION] FWD yaw feedback enabled -> удерживаем курс по гироскопу и энкодерам");
    } else {
      Serial.println("[MOTION] FWD yaw feedback disabled -> параметр useGyroStraight=OFF, корректируемся только по энкодерам");
      Serial.println("[MOTION] FWD gyro watchdog отключён на время манёвра -> избегаем ложных timeout-предупреждений");
    }
  }

  // Подаём вперёд
  if (std::fabs(trimForward) > 1e-3f) {
    Serial.printf("[MOTION] FWD trim активен: cfg=%.2f -> стартовые duty L=%d R=%d\n",
                  trimForward,
                  dutyL,
                  dutyR);
  }
  left_forward (dutyL);
  right_forward(dutyR);

  uint32_t tlog = millis();
  const float targetYaw = (allowYawForStraight && gyroConfigured)
                            ? current_heading_deg()
                            : 0.0f;
  while (true) {
    if (gAbortRequested) {
      Serial.println("[MOTION] FWD abort requested -> аккуратное завершение цикла");
      abortedByStall = true;
      break;
    }

    // Текущее состояние энкодеров (накопленные тики)
    long l = enc_peek(enc_left());
    long r = enc_peek(enc_right());

    bool leftDone  = (l >= tL);
    bool rightDone = (r >= tR);

    if (leftDone && rightDone) break;

    const uint32_t nowMs = millis();
    if (useGyroForStraight) {
      if (update_gyro()) {
        gLastGyroReadMs = nowMs;
      }
    }

    if (progressMonitor.update(l, r, nowMs)) {
      gLastProgressMs = progressMonitor.last_progress_ms;
    }

    // Ошибка выравнивания
    const ProgressError progress = compute_progress_error(l, tL, r, tR);
    const long err = progress.scaled_error_ticks;

    float yawErr = 0.0f;
    float yawIntegral = 0.0f;
    const bool yawDataReady = useGyroForStraight && !gYaw.is_idle();
    if (yawDataReady) {
      yawErr      = gYaw.yaw_deg_unwrapped() - targetYaw;
      yawIntegral = yawPi.update(yawErr, nowMs);
    }

    const auto correction = Motion::compute_straight_correction(P.kp_straight_enc,
                                                                P.kp_straight_gyro,
                                                                P.ki_straight_gyro,
                                                                err,
                                                                yawErr,
                                                                yawIntegral,
                                                                yawDataReady,
                                                                static_cast<float>(CORR_CLAMP));
    const int corr = correction.duty_delta;

    int newL = dutyBase - corr;
    int newR = dutyBase + corr;

    if (!leftDone) {
      newL = apply_directional_trim(newL, trimForward, true);
    }
    if (!rightDone) {
      newR = apply_directional_trim(newR, trimForward, false);
    }

    // Независимый стоп достигшего колеса
    if (leftDone)  newL = 0;
    if (rightDone) newR = 0;

    // Мёртвая зона + кламп
    newL = (newL > 0) ? apply_deadband(clamp_pwm(newL)) : 0;
    newR = (newR > 0) ? apply_deadband(clamp_pwm(newR)) : 0;

    // Применяем, если изменилось
    if (newL != dutyL) { dutyL = newL; left_forward (dutyL); }
    if (newR != dutyR) { dutyR = newR; right_forward(dutyR); }

    // Логирование
    if (millis() - tlog >= LOG_EVERY_MS) {
      const float yawIntegralValue = useGyroForStraight ? yawPi.value() : 0.0f;
      Serial.printf("[FWD] l=%ld/%ld r=%ld/%ld progL=%.3f progR=%.3f err=%ld enc=%.2f yawP=%.2f yawI=%.2f iState=%.2f corr=%d gyro=%s dutyL=%d%s dutyR=%d%s yawWrap=%.2f°\n",
                    l, tL, r, tR, progress.progress_left, progress.progress_right, err,
                    correction.encoder_term, correction.yaw_term, correction.integral_term,
                    yawIntegralValue, corr,
                    correction.gyro_contribution_used ? "ON" : "OFF",
                    dutyL, leftDone ? "[HOLD]" : "",
                    dutyR, rightDone ? "[HOLD]" : "",
                    gYaw.yaw_deg());
      tlog = millis();
    }

    const bool progressTimedOut = progressMonitor.is_timed_out(nowMs, P.stuck_timeout_ms);
    if (useGyroForStraight) {
      if (check_stuck(nowMs) && progressTimedOut) {
        Serial.println("[MOTION] FWD aborted: gyro detected stall");
        abortedByStall = true;
        break;
      }
    } else if (progressTimedOut) {
      Serial.println("[MOTION] FWD aborted: encoder progress timeout -> остановка для безопасности");
      abortedByStall = true;
      break;
    }

    delay(5); // сглаживание цикла
  }

  // Гасим всё
  left_coast();
  right_coast();
  if (gAbortRequested) {
    Serial.println("[MOTION] FWD done: остановлено по запросу оператора");
  } else if (abortedByStall) {
    Serial.println("[MOTION] FWD done with stall warning");
  } else {
    Serial.println("[MOTION] FWD done");
  }
  gAbortRequested = false;
}

void backward_m(float meters, int dutyBase) {
  // === Расчёт целевых тиков ===
  const float C_mm   = PI * P.wheel_d_mm;
  const float M_mm   = meters * 1000.0f;
  const long  tL     = (long)roundf((M_mm / C_mm) * (float)P.tpr_left);
  const long  tR     = (long)roundf((M_mm / C_mm) * (float)P.tpr_right);

  enc_get_and_clear(enc_left());
  enc_get_and_clear(enc_right());

  Serial.printf("[MOTION] BACK %.3fm -> L:%ld ticks, R:%ld ticks, dutyBase=%d\n", meters, tL, tR, dutyBase);

  // Комментарий: аналогично прямому ходу, сбрасываем счётчик прогресса перед
  // движением назад, чтобы анти-застревание не сработало от прошлых пауз.
  const uint32_t startNow = millis();
  gLastProgressMs = startNow;
  Serial.printf("[MOTION] stall watchdog armed at %lu ms\n", static_cast<unsigned long>(startNow));

  const float trimBackward = P.duty_trim_backward;
  int dutyL = apply_directional_trim(dutyBase, trimBackward, true);
  int dutyR = apply_directional_trim(dutyBase, trimBackward, false);
  dutyL = (dutyL > 0) ? apply_deadband(clamp_pwm(dutyL)) : 0;
  dutyR = (dutyR > 0) ? apply_deadband(clamp_pwm(dutyR)) : 0;
  bool abortedByStall = false;

  Motion::EncoderProgressMonitor progressMonitor;
  progressMonitor.reset(startNow);

  const bool gyroConfigured = P.enable_gyro_feedback && P.mpu_i2c_addr != 0;
  const bool allowYawForStraight = P.use_gyro_for_straight; // Используем пользовательский флаг и для обратного хода.
  const bool useGyroForStraight = gyroConfigured && allowYawForStraight;
  Motion::YawIntegralAccumulator yawPi;
  if (useGyroForStraight) {
    yawPi.set_limit(P.yaw_integral_limit);
    yawPi.reset(startNow);
  }

  if (gyroConfigured) {
    if (useGyroForStraight) {
      Serial.println("[MOTION] BACK yaw feedback enabled -> удерживаем курс по гироскопу и энкодерам");
    } else {
      Serial.println("[MOTION] BACK yaw feedback disabled -> параметр useGyroStraight=OFF, корректируемся только по энкодерам");
      Serial.println("[MOTION] BACK gyro watchdog отключён на время манёвра -> логи без лишних timeout-предупреждений");
    }
  }

  if (std::fabs(trimBackward) > 1e-3f) {
    Serial.printf("[MOTION] BACK trim активен: cfg=%.2f -> стартовые duty L=%d R=%d\n",
                  trimBackward,
                  dutyL,
                  dutyR);
  }
  left_backward (dutyL);
  right_backward(dutyR);

  uint32_t tlog = millis();
  const float targetYaw = (allowYawForStraight && gyroConfigured)
                            ? current_heading_deg()
                            : 0.0f;
  while (true) {
    if (gAbortRequested) {
      Serial.println("[MOTION] BACK abort requested -> аккуратное завершение цикла");
      abortedByStall = true;
      break;
    }

    long l = enc_peek(enc_left());
    long r = enc_peek(enc_right());

    bool leftDone  = (l >= tL);
    bool rightDone = (r >= tR);

    if (leftDone && rightDone) break;

    const uint32_t nowMs = millis();
    if (useGyroForStraight) {
      if (update_gyro()) {
        gLastGyroReadMs = nowMs;
      }
    }
    if (progressMonitor.update(l, r, nowMs)) {
      gLastProgressMs = progressMonitor.last_progress_ms;
    }

    const ProgressError progress = compute_progress_error(l, tL, r, tR);
    const long err = progress.scaled_error_ticks;

    float yawErr = 0.0f;
    float yawIntegral = 0.0f;
    const bool yawDataReady = useGyroForStraight && !gYaw.is_idle();
    if (yawDataReady) {
      yawErr      = gYaw.yaw_deg_unwrapped() - targetYaw;
      yawIntegral = yawPi.update(yawErr, nowMs);
    }

    const auto correction = Motion::compute_straight_correction(P.kp_straight_enc,
                                                                P.kp_straight_gyro,
                                                                P.ki_straight_gyro,
                                                                err,
                                                                yawErr,
                                                                yawIntegral,
                                                                yawDataReady,
                                                                static_cast<float>(CORR_CLAMP));
    const int corr = correction.duty_delta;

    int newL = dutyBase - corr;
    int newR = dutyBase + corr;

    if (!leftDone) {
      newL = apply_directional_trim(newL, trimBackward, true);
    }
    if (!rightDone) {
      newR = apply_directional_trim(newR, trimBackward, false);
    }

    if (leftDone)  newL = 0;
    if (rightDone) newR = 0;

    newL = (newL > 0) ? apply_deadband(clamp_pwm(newL)) : 0;
    newR = (newR > 0) ? apply_deadband(clamp_pwm(newR)) : 0;

    if (newL != dutyL) { dutyL = newL; left_backward (dutyL); }
    if (newR != dutyR) { dutyR = newR; right_backward(dutyR); }

    if (millis() - tlog >= LOG_EVERY_MS) {
      const float yawIntegralValue = useGyroForStraight ? yawPi.value() : 0.0f;
      Serial.printf("[BACK] l=%ld/%ld r=%ld/%ld progL=%.3f progR=%.3f err=%ld enc=%.2f yawP=%.2f yawI=%.2f iState=%.2f corr=%d gyro=%s dutyL=%d%s dutyR=%d%s yawWrap=%.2f°\n",
                    l, tL, r, tR, progress.progress_left, progress.progress_right, err,
                    correction.encoder_term, correction.yaw_term, correction.integral_term,
                    yawIntegralValue, corr,
                    correction.gyro_contribution_used ? "ON" : "OFF",
                    dutyL, leftDone ? "[HOLD]" : "",
                    dutyR, rightDone ? "[HOLD]" : "",
                    gYaw.yaw_deg());
      tlog = millis();
    }

    const bool progressTimedOut = progressMonitor.is_timed_out(nowMs, P.stuck_timeout_ms);
    if (useGyroForStraight) {
      if (check_stuck(nowMs) && progressTimedOut) {
        Serial.println("[MOTION] BACK aborted: gyro detected stall");
        abortedByStall = true;
        break;
      }
    } else if (progressTimedOut) {
      Serial.println("[MOTION] BACK aborted: encoder progress timeout -> остановка для безопасности");
      abortedByStall = true;
      break;
    }

    delay(5);
  }

  left_coast();
  right_coast();
  if (gAbortRequested) {
    Serial.println("[MOTION] BACK done: остановлено по запросу оператора");
  } else if (abortedByStall) {
    Serial.println("[MOTION] BACK done with stall warning");
  } else {
    Serial.println("[MOTION] BACK done");
  }
  gAbortRequested = false;
}

void rotate_deg_enc(float angle_deg, int dutyBase) {
  float arc_m = (3.1415926f * (P.wheel_base_mm / 1000.0f) * fabs(angle_deg)) / 360.0f;
  long  tL    = meters_to_ticks_left (arc_m);
  long  tR    = meters_to_ticks_right(arc_m);

  enc_get_and_clear(enc_left());
  enc_get_and_clear(enc_right());

  bool leftBack = (angle_deg > 0); // +влево: левое назад, правое вперёд
  Serial.printf("[MOTION] TURN %s %.1f° -> arc=%.3fm L:%ld R:%ld dutyBase=%d\n",
                (angle_deg>0?"LEFT":"+RIGHT"), fabs(angle_deg), arc_m, tL, tR, dutyBase);

  // Комментарий: перед началом поворота тоже обновляем момент последнего
  // «прогресса», чтобы защита от застревания не реагировала на паузы перед
  // поворотом и давала мотору стартовый люфт.
  const uint32_t startNow = millis();
  gLastProgressMs = startNow;
  Serial.printf("[MOTION] stall watchdog armed at %lu ms\n", static_cast<unsigned long>(startNow));

  int dutyL = dutyBase, dutyR = dutyBase;
  Motion::YawIntegralAccumulator yawPi; // Интеграл курса помогает «дожимать» поворот
  yawPi.set_limit(P.yaw_integral_limit);
  yawPi.reset(startNow);
  if (leftBack) { left_backward(dutyL); right_forward(dutyR); }
  else          { left_forward (dutyL); right_backward(dutyR); }

  uint32_t tlog = millis();
  const bool useGyro = P.enable_gyro_feedback && P.mpu_i2c_addr != 0;
  const float startYaw         = current_heading_deg();
  const float targetYaw        = startYaw + angle_deg;
  const float startYawWrapped  = wrap_heading_deg(startYaw);
  const float targetYawWrapped = wrap_heading_deg(targetYaw);
  Serial.printf("[TURN] yaw target prepared: start=%.2f° (wrap=%.2f°) target=%.2f° (wrap=%.2f°) direction=%s\n",
                startYaw,
                startYawWrapped,
                targetYaw,
                targetYawWrapped,
                leftBack ? "LEFT" : "RIGHT");
  while (true) {
    if (gAbortRequested) {
      Serial.println("[MOTION] TURN abort requested -> выходим до достижения цели");
      break;
    }

    long l = labs(enc_peek(enc_left()));
    long r = labs(enc_peek(enc_right()));
    const uint32_t nowMs = millis();

    bool ticksReached = (l >= tL && r >= tR);

    float yawErr = 0.0f;
    float yawIntegral = 0.0f;
    bool gyroReady = false;
    if (useGyro) {
      if (update_gyro()) {
        gLastGyroReadMs = nowMs;
      }
      if (!gYaw.is_idle()) {
        const float currentYawUnwrapped = gYaw.yaw_deg_unwrapped();
        // Комментарий: возвращаемся к классической формуле «цель минус текущее»,
        // потому что PI-регулятор настроен именно на такой знак ошибки. Пока
        // значение положительно, мы считаем, что робот недокрутил заданный угол
        // и нужно поддать газу. Как только ошибка становится отрицательной,
        // поворот прошёл дальше цели, и регулятор обязан притормозить.
        yawErr = targetYaw - currentYawUnwrapped;
        yawIntegral = yawPi.update(yawErr, nowMs);
        gyroReady = true;
      }
    }

    const bool angleReached = gyroReady && (fabs(yawErr) <= P.heading_tolerance_deg);
    if (ticksReached || angleReached) {
      if (ticksReached && !angleReached && gyroReady) {
        Serial.printf("[TURN] encoder finish, yawErr=%.2f° -> финальное дожатие\n", yawErr);
      }
      if (angleReached) {
        Serial.printf("[TURN] gyro finish yaw=%.2f° wrap=%.2f° target=%.2f° err=%.2f°\n",
                      gYaw.yaw_deg_unwrapped(),
                      gYaw.yaw_deg(),
                      targetYaw,
                      yawErr);
      }
      if (!ticksReached) {
        Serial.println("[TURN] остановлено по гироскопу до достижения тиковой цели");
      }
      break;
    }

    const ProgressError progress = compute_progress_error(l, tL, r, tR);
    const long err = progress.scaled_error_ticks;

    const TurnControlOutput turn = compute_turn_control(static_cast<float>(dutyBase),
                                                        P.kp_turn_enc,
                                                        P.kp_turn_gyro,
                                                        P.ki_turn_gyro,
                                                        err,
                                                        yawErr,
                                                        yawIntegral,
                                                        leftBack,
                                                        static_cast<float>(PWM_MAX));

    int newL = (turn.duty_left > 0.5f)
                 ? apply_deadband(clamp_pwm(static_cast<int>(std::lround(turn.duty_left))))
                 : 0;
    int newR = (turn.duty_right > 0.5f)
                 ? apply_deadband(clamp_pwm(static_cast<int>(std::lround(turn.duty_right))))
                 : 0;

    if (newL != dutyL) {
      dutyL = newL;
      if (leftBack) ledcWrite(CH_L_BACK, dutyL); else ledcWrite(CH_L_FWD, dutyL);
    }
    if (newR != dutyR) {
      dutyR = newR;
      if (leftBack) ledcWrite(CH_R_FWD, dutyR); else ledcWrite(CH_R_BACK, dutyR);
    }

    if (millis() - tlog >= LOG_EVERY_MS) {
      Serial.printf("[TURN] l=%ld/%ld r=%ld/%ld progL=%.3f progR=%.3f err=%ld yawErr=%.2f yawI=%.2f yawBoost=%.2f diff=%.2f scale=%.2f dutyL=%d dutyR=%d wrapYaw=%.2f°\n",
                    l, tL, r, tR,
                    progress.progress_left,
                    progress.progress_right,
                    err,
                    yawErr,
                    yawPi.value(),
                    turn.yaw_component,
                    turn.diff_component,
                    turn.scale_factor,
                    dutyL,
                    dutyR,
                    gYaw.yaw_deg());
      tlog = millis();
    }
    delay(5);
  }
  stop_all();
  if (gAbortRequested) {
    Serial.println("[MOTION] TURN done: остановлено по запросу оператора");
  } else {
    Serial.println("[MOTION] TURN done");
  }
  gAbortRequested = false;
}

// === Инициализация ===
bool init(const Params& p) {
  const Params prev = P; // сохраняем предыдущие значения для анализа изменений.
  const bool firstInit = !gPwmConfigured;
  const bool hardwareChanged = firstInit || !hardware_fields_equal(p, prev);

  bool ok = true;
  if (hardwareChanged) {
    if (!firstInit) {
      // --- Перед повторной настройкой корректно освобождаем старые привязки. ---
      const auto detach_pin = [](int pin) {
        if (pin >= 0) {
          ledcDetachPin(pin);
        }
      };
      detach_pin(prev.pin_left_pwm_fwd);
      detach_pin(prev.pin_left_pwm_back);
      detach_pin(prev.pin_right_pwm_fwd);
      detach_pin(prev.pin_right_pwm_back);

      if (prev.use_left_enable) {
        // Возвращаем enable-пины в безопасное состояние, чтобы избежать ложных
        // срабатываний драйверов при повторной конфигурации.
        if (prev.pin_left_en_a >= 0) digitalWrite(prev.pin_left_en_a, LOW);
        if (prev.pin_left_en_b >= 0) digitalWrite(prev.pin_left_en_b, LOW);
      }
    }

    // --- Полная настройка PWM-каналов, когда меняются аппаратные параметры. ---
    const double freqLf  = ledcSetup(CH_L_FWD,  p.pwm_freq_hz, p.pwm_res_bits);
    const double freqLb  = ledcSetup(CH_L_BACK, p.pwm_freq_hz, p.pwm_res_bits);
    const double freqRf  = ledcSetup(CH_R_FWD,  p.pwm_freq_hz, p.pwm_res_bits);
    const double freqRb  = ledcSetup(CH_R_BACK, p.pwm_freq_hz, p.pwm_res_bits);
    ok = (freqLf > 0.0) && (freqLb > 0.0) && (freqRf > 0.0) && (freqRb > 0.0);

    if (!ok) {
      Serial.println(F("[MOTION] ошибка: ledcSetup вернул 0 — проверьте частоту/разрядность PWM"));
      gPwmConfigured = false;
      Serial.println(F("[MOTION] init: завершено с ошибкой, см. сообщения выше"));
      return false;
    }

    const auto attach_pin = [&](int pin, int channel, const char* tag) {
      if (pin < 0) {
        Serial.printf("[MOTION] ошибка: не указан пин для %s (значение < 0)\n", tag);
        ok = false;
        return;
      }
      ledcAttachPin(pin, channel);
    };
    attach_pin(p.pin_left_pwm_fwd,   CH_L_FWD,  "left PWM forward");
    attach_pin(p.pin_left_pwm_back,  CH_L_BACK, "left PWM backward");
    attach_pin(p.pin_right_pwm_fwd,  CH_R_FWD,  "right PWM forward");
    attach_pin(p.pin_right_pwm_back, CH_R_BACK, "right PWM backward");

    if (ok && p.use_left_enable) {
      if (p.pin_left_en_a >= 0) {
        pinMode(p.pin_left_en_a, OUTPUT);
        digitalWrite(p.pin_left_en_a, LOW);
      } else {
        Serial.println(F("[MOTION] предупреждение: use_left_enable=ON, но pin_left_en_a < 0"));
        ok = false;
      }
      if (p.pin_left_en_b >= 0) {
        pinMode(p.pin_left_en_b, OUTPUT);
        digitalWrite(p.pin_left_en_b, LOW);
      } else {
        Serial.println(F("[MOTION] предупреждение: use_left_enable=ON, но pin_left_en_b < 0"));
        ok = false;
      }
    }

    if (!ok) {
      Serial.println(F("[MOTION] init: ошибки при привязке пинов, настройка прервана"));
      gPwmConfigured = false;
      return false;
    }

    gPwmConfigured = true;
    Serial.println(F("[MOTION] init: аппаратная конфигурация PWM обновлена"));
  } else {
    // --- Аппаратные параметры не менялись: просто переиспользуем существующие таймеры. ---
    Serial.println(F("[MOTION] init: аппаратные PWM-настройки не изменились, переиспользуем существующие каналы"));
    ok = gPwmConfigured;
    if (!ok) {
      Serial.println(F("[MOTION] init: ранее PWM не были сконфигурированы, требуется полный запуск"));
      return false;
    }
  }

  const bool resetHeading = hardwareChanged;
  apply_cached_params(p, resetHeading, "init");
  if (!resetHeading && gGyroConfigured) {
    Serial.println(F("[MOTION] init: курс сохранён, так как аппаратные параметры не менялись"));
  }
  Serial.println(F("[MOTION] init: завершено успешно"));
  return true;
}

/**
 * \brief Обновляет параметры движения без переинициализации PWM.
 *
 * Когда оператор меняет коэффициенты регуляторов или геометрию шасси через
 * веб-интерфейс, аппаратные настройки (пины, частоты) остаются неизменными.
 * Чтобы не трогать таймеры LEDC и не ловить ошибки повторной инициализации,
 * мы проверяем, что «жёсткие» поля совпадают, и просто обновляем кеш Motion.
 */
bool update_runtime(const Params& p) {
  const Params prev = P;
  if (!gPwmConfigured) {
    Serial.println(F("[MOTION] runtime update: PWM ещё не инициализированы, выполняем полную инициализацию"));
    return init(p);
  }

  if (!hardware_fields_equal(p, prev)) {
    Serial.println(F("[MOTION] runtime update: обнаружены изменения аппаратных полей, требуется полная переинициализация"));
    return false;
  }

  Serial.println(F("[MOTION] runtime update: обновляем параметры без перенастройки PWM"));
  apply_cached_params(p, /*resetHeading=*/false, "runtime update");
  if (gGyroConfigured) {
    Serial.println(F("[MOTION] runtime update: yaw-интегратор сохранён — курс не сбрасывался"));
  } else {
    Serial.println(F("[MOTION] runtime update: гироскоп отключён, обновили только коэффициенты движения"));
  }
  Serial.println(F("[MOTION] runtime update: завершено"));
  return true;
}

const Params& params() { return P; }

void configure_gyro(uint8_t addr, bool enable) {
  P.mpu_i2c_addr = addr;
  P.enable_gyro_feedback = enable && addr != 0;
  gGyroConfigured = P.enable_gyro_feedback;
  Serial.printf("[MOTION] gyro configure: enable=%s addr=0x%02X\n",
                gGyroConfigured ? "yes" : "no", addr);
  gYaw.configure_bias(gGyroConfigured,
                      P.gyro_bias_alpha,
                      P.gyro_bias_threshold_dps,
                      P.gyro_bias_settle_ms);
  reset_heading(0.0f);
}

void reset_heading(float yaw_deg) {
  const uint32_t now = millis();
  gYaw.reset(yaw_deg, now);
  gLastGyroReadMs = now;
  gLastProgressMs = now;
  Serial.printf("[MOTION] gyro heading reset -> %.2f° (wrap=%.2f°)\n",
                yaw_deg,
                wrap_heading_deg(yaw_deg));
}

bool update_gyro() {
  if (!gGyroConfigured || P.mpu_i2c_addr == 0) {
    return false;
  }
  MPUReading mpu{};
  if (!mpu_read(P.mpu_i2c_addr, mpu)) {
    return false;
  }
  const uint32_t now = millis();
  gYaw.update(mpu.gz_dps, now);
  gLastGyroReadMs = now;
  return true;
}

float current_heading_deg() {
  return gYaw.yaw_deg_unwrapped();
}

float current_heading_deg_wrapped() {
  return gYaw.yaw_deg();
}

float current_turn_rate_dps() {
  return gYaw.last_rate_dps();
}

float current_gyro_bias_dps() {
  return gYaw.bias_dps();
}

bool check_stuck(uint32_t now_ms) {
  if (!gGyroConfigured) {
    return false;
  }
  if (now_ms - gLastGyroReadMs > P.stuck_timeout_ms) {
    Serial.println("[MOTION] gyro timeout -> возможная потеря данных");
    return true;
  }
  return gYaw.is_stuck(now_ms, P.stuck_rate_threshold_dps, P.stuck_timeout_ms);
}

void request_abort() {
  gAbortRequested = true;
  Serial.println("[MOTION] abort flag установлен: ожидание мягкой остановки");
}

void clear_abort_request() {
  if (gAbortRequested) {
    Serial.println("[MOTION] abort flag сброшен вручную");
  }
  gAbortRequested = false;
}

bool is_abort_requested() {
  return gAbortRequested;
}

} // namespace Motion
