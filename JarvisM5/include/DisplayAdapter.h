#pragma once
#include <M5GFX.h>   // для LGFX_Sprite

// единственный off-screen буфер (определяется в main.cpp)
extern LGFX_Sprite frame;

// перенаправляем все вызовы рисования в буфер
inline LGFX_Sprite &gfx = frame;

// цвета «1-битной» палитры
constexpr uint16_t COL_ON  = TFT_WHITE;
constexpr uint16_t COL_OFF = TFT_BLACK;

// обёртки аналогов U8g2
inline void drawHLine(int16_t x, int16_t y, int16_t w, uint16_t c = COL_ON) {
  gfx.drawFastHLine(x, y, w, c);
}
inline void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c = COL_ON) {
  gfx.fillRect(x, y, w, h, c);
}
inline void fillTriangle(int16_t x0, int16_t y0,
                         int16_t x1, int16_t y1,
                         int16_t x2, int16_t y2,
                         uint16_t c = COL_ON) {
  gfx.fillTriangle(x0, y0, x1, y1, x2, y2, c);
}
inline uint16_t mapColor(int on) {
  return on ? COL_ON : COL_OFF;
}
