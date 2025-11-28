#include <Arduino.h>
#include "encoder_dual.h"
#include "sensors.h"
#include "motion.h"
#include "config_store.h"
#include "remote_control.h"
#include "audio_capture.h"
#include "audio_playback.h"

#ifdef ARDUINO
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#endif

// ===== Энкодеры =====
// Энкодер1: был на GPIO14
constexpr int PIN_ENC1 = 14;
// Энкодер2: ваш D4 = GPIO4
constexpr int PIN_ENC2 = 4;

// ===== Микрофоны I2S (INMP441) =====
constexpr int PIN_I2S_WS   = 5;  ///< Линия выбора канала (LRCLK/WS) для I2S1 — перенесена с GPIO27 ради освобождения высоких пинов под ЦАП.
constexpr int PIN_I2S_BCLK = 18; ///< Тактовая линия BCLK (SCK) для I2S1 — перенесена с GPIO26, чтобы развязать микрофоны и аудиовывод.
constexpr int PIN_I2S_SD   = 19; ///< Линия данных микрофонов (SD) для I2S1 — перенесена с GPIO25, что исключает конфликт с DAC1 (GPIO25).
constexpr float MIC_SPACING_M = 0.15f; ///< Расстояние между левым и правым микрофоном, м (15 см по макету).

// ===== Потоковое аудио =====
constexpr const char* AUDIO_STREAM_ENDPOINT = "ws://192.168.31.231:8765/"; ///< WebSocket-адрес приёмника аудио.
constexpr const char* AUDIO_STREAM_AUTH     = "";                             ///< Заголовок авторизации, если нужен.
constexpr uint32_t AUDIO_STREAM_HANDSHAKE_TIMEOUT_MS = 4000; ///< Таймаут ожидания рукопожатия WebSocket, мс.
constexpr uint32_t AUDIO_STREAM_RECONNECT_INTERVAL_MS = 3000; ///< Период автоповторного подключения, мс.
constexpr uint32_t AUDIO_STREAM_PING_INTERVAL_MS = 15000;     ///< Интервал поддерживающих ping-сообщений, мс.

// ===== Сервисы/датчики =====
static I2CScanResult gScan;
static float gRshunt = 0.1f;
static float gImax   = 3.2f;

// ===== Преобразования =====
inline float ticks_per_sec_to_rpm(long tps, long TPR) {
  return (TPR > 0) ? (tps * 60.0f) / (float)TPR : 0.0f;
}
inline float rpm_to_mps(float rpm, float D_mm) {
  float C_mm = 3.1415926f * D_mm;
  return (C_mm / 1000.0f) * (rpm / 60.0f);
}
inline float ticks_to_m(float ticks, long TPR, float D_mm) {
  if (TPR <= 0) return 0.0f;
  float C_mm = 3.1415926f * D_mm;
  return (ticks * C_mm) / (float)TPR / 1000.0f;
}
inline float ticks_to_m_side(long ticks, long TPR, float wheel_d_mm) {
  if (TPR <= 0) return 0.0f;
  const float C_mm = 3.1415926f * wheel_d_mm;
  return (ticks * C_mm) / (float)TPR / 1000.0f;
}

static bool calib1 = false, calib2 = false;
static long calib1_start = 0, calib2_start = 0;

#ifdef ARDUINO
namespace {

TaskHandle_t gRemoteMotionTaskHandle = nullptr; ///< Текущая задача, выполняющая манёвр из веб-интерфейса.
RemoteControl::Command gRemoteMotionCommand{}; ///< Буфер с параметрами активной команды.

/**
 * \brief Выполняет команду движения в том же виде, как раньше делал основной цикл.
 *
 * Функция содержит подробные логи, чтобы сохранить наглядность в Serial-мониторе
 * и облегчить анализ поведения робота при удалённом управлении.
 */
void execute_remote_motion(const RemoteControl::Command& cmd) {
  const auto& params = Motion::params();
  const int pwmMax = (1 << params.pwm_res_bits) - 1;
  const int dutyDefaultFwd  = (pwmMax * 8) / 10;  // ~80% шкалы
  const int dutyDefaultTurn = (pwmMax * 7) / 10;  // ~70% шкалы
  const int dutyToUse = (cmd.duty > 0)
                          ? cmd.duty
                          : (cmd.action == RemoteControl::Action::Rotate ? dutyDefaultTurn : dutyDefaultFwd);

  switch (cmd.action) {
    case RemoteControl::Action::Move: {
      if (cmd.direction == RemoteControl::Direction::Forward) {
        Serial.printf("[REMOTE] Выполняем движение вперёд на %.3f м duty=%d (асинхронно)\n", cmd.value, dutyToUse);
        Motion::forward_m(cmd.value, dutyToUse);
      } else {
        Serial.printf("[REMOTE] Выполняем движение назад на %.3f м duty=%d (асинхронно)\n", cmd.value, dutyToUse);
        Motion::backward_m(cmd.value, dutyToUse);
      }
      break;
    }
    case RemoteControl::Action::Rotate: {
      const float angle = (cmd.direction == RemoteControl::Direction::Forward) ? cmd.value : -cmd.value;
      Serial.printf("[REMOTE] Выполняем поворот на %.1f° duty=%d (асинхронно)\n", angle, dutyToUse);
      Motion::rotate_deg_enc(angle, dutyToUse);
      break;
    }
    case RemoteControl::Action::EmergencyStop:
      Serial.println("[REMOTE] предупреждение: execute_remote_motion вызван с аварийной командой");
      break;
  }

  Motion::clear_abort_request();
}

/**
 * \brief Задача FreeRTOS, исполняющая длительную команду, пока основной loop() обслуживает телеметрию.
 */
void remote_motion_task(void* param) {
  const RemoteControl::Command cmd = *static_cast<RemoteControl::Command*>(param);
  Serial.println("[REMOTE] задача выполнения движения стартовала");
  execute_remote_motion(cmd);
  gRemoteMotionTaskHandle = nullptr;
  RemoteControl::notify_command_complete();
  Serial.println("[REMOTE] задача выполнения движения завершена");
  vTaskDelete(nullptr);
}

/**
 * \brief Стартует фоновую задачу движения; при ошибке возвращает false.
 */
bool start_remote_motion_task(const RemoteControl::Command& cmd) {
  if (gRemoteMotionTaskHandle != nullptr) {
    Serial.println("[REMOTE] невозможно стартовать новую задачу: предыдущая ещё активна");
    return false;
  }

  gRemoteMotionCommand = cmd;
  Motion::clear_abort_request();

  constexpr uint32_t STACK_WORDS = 8192; // увеличенный стек для обильных логов
  const BaseType_t created = xTaskCreatePinnedToCore(
      remote_motion_task,
      "RemoteMotion",
      STACK_WORDS,
      &gRemoteMotionCommand,
      1,
      &gRemoteMotionTaskHandle,
      1);

  if (created != pdPASS) {
    gRemoteMotionTaskHandle = nullptr;
    Serial.println("[REMOTE] ошибка: не удалось создать задачу удалённого движения");
    return false;
  }

  Serial.println("[REMOTE] задача удалённого движения успешно создана");
  return true;
}

} // namespace
#else
// На десктопных сборках (юнит-тесты) мы оставляем заглушку, чтобы код компилировался.
static void execute_remote_motion(const RemoteControl::Command&) {}
#endif

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== Boot ===");

  // I2C + датчики
  i2c_begin(21, 22, 400000);
  gScan = i2c_scan_sensors();
  Serial.printf("[I2C] INA@0x%02X  MPU@0x%02X\n", gScan.ina219_addr, gScan.mpu_addr);
  if (gScan.ina219_addr && ina219_init(gScan.ina219_addr, gRshunt, gImax))
    Serial.println("[INA] init OK");
  else
    Serial.println("[INA] not found/FAIL");

  bool gyroReady = false;
  if (gScan.mpu_addr && mpu_init(gScan.mpu_addr)) {
    Serial.println("[MPU] init OK");
    gyroReady = true;
  } else {
    Serial.println("[MPU] not found/FAIL");
  }

  // --- Motion (движение) ---
  Motion::Params mp;
  mp.pwm_freq_hz       = 5000;
  mp.pwm_res_bits      = 10;

  // Назначение пинов под «вперёд/назад»:
  mp.pin_left_pwm_fwd   = 2;   // PIN2_R_PWM
  mp.pin_left_pwm_back  = 15;  // PIN2_L_PWM
  mp.pin_right_pwm_fwd  = 13;  // PIN1_R_PWM
  mp.pin_right_pwm_back = 12;  // PIN1_L_PWM

  ConfigStore::Environment cfgEnv{};
  cfgEnv.gyro_available = gyroReady;
  cfgEnv.mpu_i2c_addr   = gyroReady ? gScan.mpu_addr : 0;
  ConfigStore::set_environment(cfgEnv);

  ConfigStore::TuningConfig tuning = ConfigStore::load_or_defaults();
  ConfigStore::apply_tuning_to_params(tuning, mp, cfgEnv);

  Serial.println("[CONFIG] применяем стартовую конфигурацию движения");
  Motion::init(mp);
  if (gyroReady) {
    // После инициализации обязательно сбрасываем курс, чтобы интегратор
    // начинал с нуля и не ловил случайные старые значения.
    Motion::reset_heading(0.0f);
  }

  // Энкодеры
  enc_init(ENC1, PIN_ENC1, /*externalPullup=*/true);
  enc_init(ENC2, PIN_ENC2, /*externalPullup=*/true);
  enc_set_min_pulse_us(80);

  Serial.println("[SYS] Ready. Commands: f1/b1/s1, f2/b2/s2, C1/E1, C2/E2");
  //setCpuFrequencyMhz(80);

  // --- Аудиомодуль: запуск I2S-микрофонов INMP441 ---
  Audio::Config audioCfg;
  audioCfg.pinWs = PIN_I2S_WS;
  audioCfg.pinBclk = PIN_I2S_BCLK;
  audioCfg.pinData = PIN_I2S_SD;
  audioCfg.sampleRate = 16000; // Микрофоны всегда работали на 16 кГц для корректного STT
  audioCfg.frameSamples = 512;
  audioCfg.microphoneSpacingMeters = MIC_SPACING_M;
  audioCfg.enableLocalization = false; // Угол будет вычисляться на сервере, чтобы не нагружать ESP32.
  if (Audio::init(audioCfg)) {
    Serial.printf("[AUDIO] Микрофоны готовы: %u Гц, %u сэмплов в кадре, база %.3f м\n",
                  audioCfg.sampleRate, static_cast<unsigned>(audioCfg.frameSamples),
                  audioCfg.microphoneSpacingMeters);
  } else {
    Serial.println("[AUDIO] ошибка: не удалось инициализировать аудиоподсистему");
  }

  AudioPlayback::Config playbackCfg{};
  playbackCfg.mode = AudioPlayback::OutputMode::Max98357aI2S; // Переключаем вывод речи на внешний I2S-усилитель MAX98357A.
  playbackCfg.defaultSampleRate = 44100; // Выводим TTS/эмоции в 44.1 кГц, независим от входа
  playbackCfg.frameSamplesHint = audioCfg.frameSamples;
  playbackCfg.queueCapacity = 6;
  playbackCfg.defaultVolume = 1.0f;
  playbackCfg.pinBclk = 26; // BCLK модуля MAX98357A.
  playbackCfg.pinLrc = 27;  // LRC/WS модуля MAX98357A.
  playbackCfg.pinDin = 25;  // DIN/SD модуля MAX98357A.
  if (AudioPlayback::init(playbackCfg)) {
    Serial.println("[PLAYBACK] MAX98357A готов принимать голосовой ответ Jarvis по WebSocket");
  } else {
    Serial.println("[PLAYBACK] ошибка: не удалось запустить тракт воспроизведения");
  }

  RemoteControl::AudioStreamConfig streamCfg{};
  streamCfg.endpoint = AUDIO_STREAM_ENDPOINT;
  streamCfg.authHeader = AUDIO_STREAM_AUTH;
  streamCfg.handshakeTimeoutMs = AUDIO_STREAM_HANDSHAKE_TIMEOUT_MS;
  streamCfg.reconnectIntervalMs = AUDIO_STREAM_RECONNECT_INTERVAL_MS;
  streamCfg.pingIntervalMs = AUDIO_STREAM_PING_INTERVAL_MS;
  RemoteControl::set_audio_stream_config(streamCfg);

  // --- Стартуем модуль удалённого управления ---
  RemoteControl::init("Redmi_A33D", "88882222");
}

void handleSerial() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  Serial.printf("[CMD] \"%s\"\r\n", line.c_str());

  if (RemoteControl::is_busy()) {
    Serial.println("[CMD] команда отклонена: робот выполняет удалённый манёвр");
    return;
  }

  // Базовый duty по умолчанию (¼ шкалы)
  const int pwmMax = (1 << Motion::params().pwm_res_bits) - 1;
  const int dutyDefaultFwd  = (pwmMax * 8) / 10;  // ~80% вместо 100%
  const int dutyDefaultTurn = (pwmMax * 7) / 10;  // ~70% для поворотов

  // Вспомогалки
  auto isTwo = [&](char a, char b){
    return (line.length() == 2 && (char)tolower(line[0]) == (char)tolower(a) && line[1] == b);
  };
  auto parseFloatAfterHead = [&](){
    // берём подстроку после 1-го символа (F/B/R/L) и парсим
    if (line.length() <= 1) return 0.0f;
    String v = line.substring(1);
    v.trim();
    return v.toFloat();
  };

  // ===== Низкоуровневые команды (по бортам) =====
  if (isTwo('f','1')) { Motion::left_forward(dutyDefaultFwd);  Serial.printf("[LOW] LEFT  forward duty=%d\n", dutyDefaultFwd);  return; }
  if (isTwo('b','1')) { Motion::left_backward(dutyDefaultFwd); Serial.printf("[LOW] LEFT  backward duty=%d\n", dutyDefaultFwd); return; }
  if (isTwo('s','1')) { Motion::left_coast();                  Serial.println("[LOW] LEFT  stop");                             return; }

  if (isTwo('f','2')) { Motion::right_forward(dutyDefaultFwd);  Serial.printf("[LOW] RIGHT forward duty=%d\n", dutyDefaultFwd);  return; }
  if (isTwo('b','2')) { Motion::right_backward(dutyDefaultFwd); Serial.printf("[LOW] RIGHT backward duty=%d\n", dutyDefaultFwd); return; }
  if (isTwo('s','2')) { Motion::right_coast();                  Serial.println("[LOW] RIGHT stop");                              return; }

  // ===== Калибровка TPR (по одному обороту) =====
  if (isTwo('C','1') || isTwo('c','1')) {
    calib1 = true; calib1_start = enc_peek(ENC1);
    Motion::left_coast();
    Serial.println("[CAL1] start: turn EXACTLY 1 rev, then E1");
    return;
  }
  if (isTwo('E','1') || isTwo('e','1')) {
    if (calib1) {
        long delta = labs(enc_peek(ENC1) - calib1_start);
        enc_get_and_clear(ENC1);
        calib1 = false;

        // --- пишем в Motion::Params с учётом enc_left_is_enc1 ---
        const auto& P = Motion::params();
        Motion::Params newP = P;                    // копия текущих параметров
        if (P.enc_left_is_enc1) newP.tpr_left  = delta;
        else                    newP.tpr_right = delta;

        Motion::init(newP); // применяем (LEDC, логи и т.д.)
        Serial.printf("[CAL1] done: ENC1=%ld -> TPR(%s)=%ld\n",
                    delta, P.enc_left_is_enc1 ? "LEFT" : "RIGHT", delta);
    } else {
        Serial.println("[CAL1] not active");
    }
    return;
  }
  if (isTwo('C','2') || isTwo('c','2')) {
    calib2 = true; calib2_start = enc_peek(ENC2);
    Motion::right_coast();
    Serial.println("[CAL2] start: turn EXACTLY 1 rev, then E2");
    return;
  }
  if (isTwo('E','2') || isTwo('e','2')) {
    if (calib2) {
        long delta = labs(enc_peek(ENC2) - calib2_start);
        enc_get_and_clear(ENC2);
        calib2 = false;

        // --- пишем в Motion::Params с учётом enc_left_is_enc1 ---
        const auto& P = Motion::params();
        Motion::Params newP = P;
        if (P.enc_left_is_enc1) newP.tpr_right = delta;
        else                    newP.tpr_left  = delta;

        Motion::init(newP);
        Serial.printf("[CAL2] done: ENC2=%ld -> TPR(%s)=%ld\n",
                    delta, P.enc_left_is_enc1 ? "RIGHT" : "LEFT", delta);
    } else {
        Serial.println("[CAL2] not active");
    }
    return;
  }

  // ===== Высокоуровневые движения =====
  char h = toupper(line[0]);

  if (h == 'F') {
    float m = parseFloatAfterHead();
    if (m > 0.0f) {
      Motion::forward_m(m, dutyDefaultFwd);
    } else {
      Serial.println("[MOVE] usage: F0.50  (meters)");
    }
    return;
  }

  if (h == 'B') {
    float m = parseFloatAfterHead();
    if (m > 0.0f) {
      Motion::backward_m(m, dutyDefaultFwd);
    } else {
      Serial.println("[MOVE] usage: B0.20  (meters)");
    }
    return;
  }

  if (h == 'L') {
    float a = parseFloatAfterHead();
    if (a > 0.0f) {
      // Положительный угол = влево (левое колесо назад, правое вперёд)
      Motion::rotate_deg_enc(+a, dutyDefaultTurn);
    } else {
      Serial.println("[TURN] usage: L90  (degrees)");
    }
    return;
  }

  if (h == 'R') {
    float a = parseFloatAfterHead();
    if (a > 0.0f) {
      // Отрицательный угол = вправо (левое вперёд, правое назад)
      Motion::rotate_deg_enc(-a, dutyDefaultTurn);
    } else {
      Serial.println("[TURN] usage: R90  (degrees)");
    }
    return;
  }

  // ===== Аварийная остановка =====
  if (h == 'X') {
    Motion::stop_all();
    Serial.println("[STOP] all motors coast");
    return;
  }

  // ===== Хелп =====
  Serial.println(
    "[HELP] low-level: f1/b1/s1  f2/b2/s2\n"
    "       calib:     C1 E1  |  C2 E2\n"
    "       move:      F<meters>  B<meters>\n"
    "       turn:      L<deg>     R<deg>\n"
    "       emergency: X"
  );
}


void loop() {
  static uint32_t tLog = millis();
  static long prevPeekEnc1 = 0;            // Накопленные тики для ENC1 между логами.
  static long prevPeekEnc2 = 0;            // Аналогично для ENC2.
  static bool telemetrySuppressed = false; // Флаг, что сброс тиков временно запрещён.

  // Обновляем курс даже в режиме ожидания, чтобы интегратор не отставал.
  if (Motion::params().enable_gyro_feedback) {
    if (!Motion::update_gyro()) {
      // При отсутствии новых данных напомним об этом в отладке.
      static uint32_t lastWarn = 0;
      const uint32_t now = millis();
      if (now - lastWarn > 500) {
        Serial.println("[GYRO] warning: нет свежих данных от MPU");
        lastWarn = now;
      }
    }
  }

  Audio::poll();

  handleSerial();
  RemoteControl::loop();

  // --- Проверяем, не пришла ли команда с веб-интерфейса ---
  RemoteControl::Command rcCmd;
  if (RemoteControl::fetch_command(rcCmd)) {
    switch (rcCmd.action) {
      case RemoteControl::Action::Move:
      case RemoteControl::Action::Rotate:
#ifdef ARDUINO
        if (!start_remote_motion_task(rcCmd)) {
          Serial.println("[REMOTE] выполняем движение синхронно из-за отказа запуска задачи");
          execute_remote_motion(rcCmd);
          RemoteControl::notify_command_complete();
        }
#else
        execute_remote_motion(rcCmd);
        RemoteControl::notify_command_complete();
#endif
        break;
      case RemoteControl::Action::EmergencyStop:
        Serial.println("[REMOTE] Аварийная остановка по запросу веб-интерфейса");
        Motion::request_abort();
#ifdef ARDUINO
        if (gRemoteMotionTaskHandle != nullptr) {
          Serial.println("[REMOTE] аварийная остановка: фоновой задаче отправлен сигнал отмены");
        }
#endif
        Motion::stop_all();
        RemoteControl::clear_commands();
        RemoteControl::notify_command_complete();
        break;
    }
  }

  if (millis() - tLog >= 500) {
    const bool allowTelemetryResets = RemoteControl::telemetry_updates_allowed();
    if (!allowTelemetryResets && !telemetrySuppressed) {
      Serial.println("[TELEMETRY] сброс энкодеров приостановлен: выполняется удалённый манёвр");
      telemetrySuppressed = true;
    } else if (allowTelemetryResets && telemetrySuppressed) {
      Serial.println("[TELEMETRY] сброс энкодеров возобновлён: удалённый манёвр завершён");
      telemetrySuppressed = false;
    }

    const auto& P = Motion::params();           // берём актуальные параметры из Motion
    const bool enc1_is_left = P.enc_left_is_enc1;

    // --- читаем пиковые и приращения, не ломая калибровку ---
    long peek1 = enc_peek(ENC1);
    long peek2 = enc_peek(ENC2);

    auto computeDelta = [](long peek, long& prev, bool allowReset, bool calib, const char* tag) {
      if (calib) {
        // Во время калибровки не трогаем счётчики: просто обновляем предыдущие значения.
        prev = peek;
        return 0L;
      }

      long delta = peek - prev;
      if (delta < 0) {
        // Подробный лог: кто-то (например, Motion::forward_m) мог обнулить энкодер.
        Serial.printf("[TELEMETRY] предупреждение: %s сброшен вне цикла, компенсируем дельту вручную\n", tag);
        delta = peek;
      }

      if (allowReset) {
        // Официально очищаем счётчик для следующего окна измерений.
        prev = 0;
      } else {
        // Сохраняем абсолютный прогресс, чтобы в следующем цикле получить корректную разницу.
        prev = peek;
      }
      return delta;
    };

    const long dt1 = computeDelta(peek1, prevPeekEnc1, allowTelemetryResets, calib1, "ENC1");
    const long dt2 = computeDelta(peek2, prevPeekEnc2, allowTelemetryResets, calib2, "ENC2");

    if (!calib1 && allowTelemetryResets) {
      enc_get_and_clear(ENC1);
    }
    if (!calib2 && allowTelemetryResets) {
      enc_get_and_clear(ENC2);
    }

    // --- переводим «лево/право» с учётом enc_left_is_enc1 ---
    long dt_left   = enc1_is_left ? dt1 : dt2;
    long dt_right  = enc1_is_left ? dt2 : dt1;
    long pk_left   = enc1_is_left ? peek1 : peek2;
    long pk_right  = enc1_is_left ? peek2 : peek1;

    // --- частоты тиков за секунду (лог каждые 0.5с) ---
    long tps_left  = dt_left  * 2;
    long tps_right = dt_right * 2;

    // --- RPM по каждой стороне с учётом TPR(L/R) из Motion ---
    float rpm_left  = (P.tpr_left  > 0) ? (tps_left  * 60.0f) / (float)P.tpr_left  : 0.0f;
    float rpm_right = (P.tpr_right > 0) ? (tps_right * 60.0f) / (float)P.tpr_right : 0.0f;

    // --- Скорости колёс (м/с) и пройденные пути (м) ---
    float v_left  = (3.1415926f * P.wheel_d_mm / 1000.0f) * (rpm_left  / 60.0f);
    float v_right = (3.1415926f * P.wheel_d_mm / 1000.0f) * (rpm_right / 60.0f);

    float S_left  = ticks_to_m_side(pk_left,  P.tpr_left,  P.wheel_d_mm);
    float S_right = ticks_to_m_side(pk_right, P.tpr_right, P.wheel_d_mm);

    // --- вывод в прежнем стиле: M1 = физический ENC1, M2 = физический ENC2 ---
    // Для каждого M? подставляем «его» TPR (левый/правый), чтобы расчёт был корректным при любом маппинге.
    long  TPR_for_M1 = enc1_is_left ? P.tpr_left  : P.tpr_right;
    long  TPR_for_M2 = enc1_is_left ? P.tpr_right : P.tpr_left;

    float rpm_M1 = ticks_per_sec_to_rpm((!calib1 ? dt1*2 : 0), TPR_for_M1);
    float rpm_M2 = ticks_per_sec_to_rpm((!calib2 ? dt2*2 : 0), TPR_for_M2);

    float v_M1 = rpm_to_mps(rpm_M1, P.wheel_d_mm);
    float v_M2 = rpm_to_mps(rpm_M2, P.wheel_d_mm);

    float S_M1 = ticks_to_m_side(peek1, TPR_for_M1, P.wheel_d_mm);
    float S_M2 = ticks_to_m_side(peek2, TPR_for_M2, P.wheel_d_mm);

    Serial.printf("[M1] dt=%ld tps=%ld rpm=%.2f v=%.3f m/s S=%.3f m TPR=%ld%s\n",
                    dt1, (!calib1 ? dt1*2 : 0), rpm_M1, v_M1, S_M1, TPR_for_M1, calib1 ? " [CAL]" : "");
    Serial.printf("[M2] dt=%ld tps=%ld rpm=%.2f v=%.3f m/s S=%.3f m TPR=%ld%s\n",
                    dt2, (!calib2 ? dt2*2 : 0), rpm_M2, v_M2, S_M2, TPR_for_M2, calib2 ? " [CAL]" : "");

    // -------- Датчики (как раньше) --------
    INA219Reading ina{};
    RemoteControl::Diagnostics diag{};
    const Audio::Diagnostics audioDiag = Audio::latest_diagnostics();
    if (gScan.ina219_addr && ina219_read(gScan.ina219_addr, ina)) {
        Serial.printf("[INA] bus=%.3f V shunt=%.2f mV current=%.3f A power=%.3f W\n",
                    ina.bus_V, ina.shunt_mV, ina.current_A, ina.power_W);
        diag.busVoltage = ina.bus_V;
        diag.currentA   = ina.current_A;
        diag.powerW     = ina.power_W;
    } else {
        Serial.println("[INA] read FAIL or not present");
    }

    MPUReading mpu{};
    if (gScan.mpu_addr && mpu_read(gScan.mpu_addr, mpu)) {
        Serial.printf("[MPU] a[g]=[%.3f %.3f %.3f] g[dps]=[%.1f %.1f %.1f] T=%.2f C\n",
                    mpu.ax_g, mpu.ay_g, mpu.az_g, mpu.gx_dps, mpu.gy_dps, mpu.gz_dps, mpu.temp_C);
        if (Motion::params().enable_gyro_feedback) {
          const float heading = Motion::current_heading_deg();
          const float rate    = Motion::current_turn_rate_dps();
          const float bias    = Motion::current_gyro_bias_dps();
          const float rawRate = rate + bias;
          Serial.printf("[GYRO] heading=%.2f° rate=%.2f°/s raw=%.2f°/s bias=%.2f°/s\n",
                        heading, rate, rawRate, bias);
          diag.headingDeg   = heading;
          diag.turnRateDps  = rate;
          diag.gyroBiasDps  = bias;
        }
        diag.temperatureC = mpu.temp_C;
    } else {
        Serial.println("[MPU] read FAIL or not present");
    }

    diag.ticksLeft      = pk_left;
    diag.ticksRight     = pk_right;
    diag.velocityLeft   = v_left;
    diag.velocityRight  = v_right;
    diag.distanceLeft   = S_left;
    diag.distanceRight  = S_right;
    diag.audioLocalizationActive = audioDiag.localizationEnabled;
    diag.audioDirectionDeg = audioDiag.directionDeg;
    diag.audioConfidence   = audioDiag.confidence;
    diag.audioRmsLeft      = audioDiag.rmsLeft;
    diag.audioRmsRight     = audioDiag.rmsRight;
    diag.audioSampleRate   = audioDiag.sampleRate;
    diag.audioFrameSamples = audioDiag.frameSamples;
    diag.audioStreamReady  = audioDiag.streamHasChunk;
    diag.audioMicSpacingMeters = audioDiag.microphoneSpacingMeters;

    if (audioDiag.localizationEnabled) {
      Serial.printf("[AUDIO] локально dir=%.1f° conf=%.2f lvl=%.3f/%.3f stream=%s\n",
                    audioDiag.directionDeg,
                    audioDiag.confidence,
                    audioDiag.rmsLeft,
                    audioDiag.rmsRight,
                    audioDiag.streamHasChunk ? "готов" : "ожидание");
    } else {
      Serial.printf("[AUDIO] серверная локализация lvl=%.3f/%.3f stream=%s\n",
                    audioDiag.rmsLeft,
                    audioDiag.rmsRight,
                    audioDiag.streamHasChunk ? "готов" : "ожидание");
    }

    RemoteControl::update_diagnostics(diag);

    tLog = millis();
  }

}
