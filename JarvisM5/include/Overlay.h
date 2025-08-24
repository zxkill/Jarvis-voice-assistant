#pragma once
#include <M5GFX.h>
#include "Config.h"
#include "Battery.h"

class Overlay {
public:
  void setTime   (String t) { time_    = std::move(t); }
  void setWeather(String w) { weather_ = std::move(w); }
  void setText   (String t);
  void setWiFiState    (bool on) { wifiConnected_ = on; }
  void setWSState      (bool on) { wsConnected_   = on; }

  void tick();                  // скрывает текст по таймауту
  void draw(LGFX_Sprite& gfx);  // рисует все слои

private:
  String time_, weather_, text_;
  bool   textVisible_{false};
  unsigned long lastTextMs_{0};
  bool wifiConnected_{false};
  bool wsConnected_{false};
};
