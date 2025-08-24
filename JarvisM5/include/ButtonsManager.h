#pragma once
#include <M5Unified.h>
#include "Config.h"
#include "Menu.h"
#include "Logger.h"

class ButtonsManager {
public:
  static ButtonsManager& instance() {
    static ButtonsManager inst;
    return inst;
  }

  // Настройка колбэков вызывается из setup()
  void init(Menu& menuRef) {
    menu = &menuRef;
    bPressTime = 0;
  }

  // Вызывать каждый цикл из loop()
  void update() {
    M5.update();

    handleLongPressB();
    if (menu->isVisible()) {
      handleMenuNavigation();
    }
  }

private:
  Menu* menu = nullptr;
  uint32_t bPressTime = 0;

  ButtonsManager() = default;

  void handleLongPressB() {
    if (M5.BtnB.isPressed()) {
      if (bPressTime == 0) {
        bPressTime = millis();
      } else if (millis() - bPressTime >= LONG_PRESS_MS && !menu->isVisible()) {
        // Открываем меню
        Logger::log(LogLevel::INFO, "Opening menu...");
        menu->begin({
          {"Enable AP", [](){
             Logger::log(LogLevel::INFO, "Reconfiguring Wi-Fi...");
             
          }}
        });
      }
    } else {
      bPressTime = 0;
    }
  }

  void handleMenuNavigation() {
    if (M5.BtnA.wasPressed()) {
      menu->navPrev();
    }
    if (M5.BtnC.wasPressed()) {
      menu->navNext();
    }
    if (M5.BtnB.wasPressed()) {
      menu->select();
    }
  }
};
