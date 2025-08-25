#include <M5Unified.h>
#include <M5GFX.h>
LGFX_Sprite frame(&M5.Display);

#include "DisplayAdapter.h"
#include "Config.h"
#include "logo.h"
#include "Logger.h"
#include "Menu.h"
#include "ButtonsManager.h"
#include "Overlay.h"
#include "FaceWrapper.h"
#include "Emotion.h"
#include "EnergyManager.h"
#include "ServoController.h"
#include "SerialClient.h"
#include "UIMode.h"

#include <esp_wifi.h>
#include <Ticker.h>

Overlay       overlay;
FaceWrapper   face(320, 240, 60);
Emotion       emotion(face);
ServoController servo;
SerialClient ser(overlay, emotion, servo);

static UIMode uiMode = UIMode::Sleep;
static bool  loggerReady = false;

void setUIMode(UIMode m) {
  uiMode = m;
  if (m == UIMode::Sleep) {
    Logger::enableScreenLogging(false);
    M5.Display.setBrightness(0);
    M5.Display.sleep();
  } else {
    if (!loggerReady) {
      Logger::init();
      Logger::enableAutoPresent(false);
      loggerReady = true;
    }
    Logger::enableScreenLogging(true);
    M5.Display.wakeup();
    M5.Display.setBrightness(20);
  }
}

UIMode getUIMode() { return uiMode; }

Menu          menu;
EnergyManager energy;

static Ticker keepAlive;
static bool networkStarted = false;

void setup() {
  // 1) Дисплей
  auto cfg = M5.config();
  cfg.clear_display = true;
  M5.begin(cfg);
  // Start with backlight off until the host tells us otherwise
  M5.Display.setBrightness(0);
  frame.setColorDepth(8);
  frame.createSprite(320, 240);
  setUIMode(UIMode::Sleep);
  Logger::log(LogLevel::INFO, "=== Device booting ===");

  // 3) Серво
  servo.begin();
  ServoController::Tuning tune;
  // Пропорция «пиксели ошибки → градусы сервы» и плавность движения.
  tune.kpYawDegPerPx   = 0.06f;  // ≈38°/640px по горизонтали
  tune.kpPitchDegPerPx = 0.10f;  // ≈62°/480px по вертикали
  tune.smoothYaw       = 0.25f;  // за цикл выполняем 25% оставшегося пути
  tune.smoothPitch     = 0.25f;
  tune.deadzoneYawPx   = 10.0f;  // зона покоя вокруг центра кадра
  tune.deadzonePitchPx = 10.0f;
  tune.invertYaw       = true;   // ← если поведение «убегает» в другую сторону — поставь false
  tune.invertPitch     = false;  // ← аналогично
  // Жёсткие пределы по углам поворота
  tune.yawMinDeg       = -70.0f; tune.yawMaxDeg   = +70.0f;
  tune.pitchMinDeg     = -65.0f; tune.pitchMaxDeg = +65.0f;
  // Если серва не по центру в нейтрали — подправь тримы
  tune.trimYawDeg      = 0.0f;
  tune.trimPitchDeg    = 0.0f;
  // Диапазон импульсов PWM, соответствующий крайним углам
  tune.minPulseUs      = 500;
  tune.maxPulseUs      = 2400;
  // Применяем настройки к контроллеру сервоприводов.
  servo.setTuning(tune);

  // 4) Кнопки
  ButtonsManager::instance().init(menu);

  // 5) Энергосбережение CPU / отключаем Wi-Fi (работаем через BT)
  setCpuFrequencyMhz(80);
  esp_wifi_stop();
  energy.begin();

  // 6) Транспорт
  ser.begin(SERIAL_BAUD);
  Logger::log(LogLevel::INFO, "[SYS] USB Serial JSON готов @%lu", (unsigned long)SERIAL_BAUD);
}

void loop() {
  ButtonsManager::instance().update();
  ser.loop();

  switch (getUIMode()) {
    case UIMode::Sleep:
      return;

    case UIMode::Boot: {
      static uint32_t last = 0;
      if (millis() - last > 100) {
        last = millis();
        frame.fillScreen(TFT_BLACK);
        frame.pushImage((320 - 128) / 2, 40, 128, 32, logo_data);
        Logger::renderTo(frame);
        frame.pushSprite(0, 0);
      }
      energy.update(true);
      return;
    }

    case UIMode::Run:
      overlay.tick();
      if (menu.isVisible()) {
        energy.update(true);
        return;
      }
      static uint32_t last = 0;
      if (millis() - last > 100) {
        last = millis();
        frame.fillScreen(0);
        face.update();
        overlay.draw(frame);
        Logger::renderTo(frame);
        frame.pushSprite(0, 0);
      }
      energy.update(true);
      return;
  }
}
