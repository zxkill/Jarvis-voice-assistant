#pragma once
#include <M5Unified.h>
#include <vector>
#include <functional>
#include "Config.h"

// Элемент меню: текст + действие
struct MenuItem {
  String label;
  std::function<void()> action;
};

class Menu {
public:
  void begin(const std::vector<MenuItem>& items) {
    menuItems = items;
    idx = 0;
    visible = true;
    lastBlink = millis();
    blinkOn = true;
    draw();
  }

  void stop() {
    visible = false;
    // очистка области меню
    gfx.fillRect(MENU_R.x, MENU_R.y, MENU_R.w, MENU_R.h, COL_BACKGROUND);
    frame.pushSprite(0,0);
  }

  bool isVisible() const { return visible; }

  void navNext() {
    if (!visible) return;
    idx = (idx + 1) % menuItems.size();
    draw();
  }
  void navPrev() {
    if (!visible) return;
    idx = (idx + menuItems.size() - 1) % menuItems.size();
    draw();
  }
  void select() {
    if (!visible) return;
    menuItems[idx].action();
    stop();
  }

private:
  std::vector<MenuItem> menuItems;
  size_t idx = 0;
  bool visible = false;
  bool blinkOn = true;
  uint32_t lastBlink = 0;
  static constexpr int lineH = 24; // высота одной строки

  void draw() {
    // фон
    gfx.fillRect(MENU_R.x, MENU_R.y, MENU_R.w, MENU_R.h, COL_BACKGROUND);
    gfx.setTextSize(2);
    for (size_t i = 0; i < menuItems.size(); ++i) {
      int y = MENU_R.y + 4 + i * lineH;
      // подсветка
      if ((int)i == idx && blinkOn) {
        gfx.fillRect(MENU_R.x+2, y-2, MENU_R.w-4, lineH, COL_HIGHLIGHT);
        gfx.setTextColor(COL_BACKGROUND);
      } else {
        gfx.setTextColor(COL_FOREGROUND);
      }
      gfx.setCursor(MENU_R.x + 4, y);
      gfx.print(menuItems[i].label);
    }
    frame.pushSprite(0,0);

    // обновляем мигание
    uint32_t now = millis();
    if (now - lastBlink >= MENU_BLINK_INTERVAL) {
      blinkOn = !blinkOn;
      lastBlink = now;
      draw();
    }
  }
};
