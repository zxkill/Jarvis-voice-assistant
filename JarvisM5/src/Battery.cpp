#include "Battery.h"

using namespace power;

static constexpr VoltSegment discharge[] {
  {4.20f, 4.07f, 100,100},
  {4.07f, 3.81f, 100, 75},
  {3.81f, 3.55f,  75, 50},
  {3.55f, 3.33f,  50, 25},
  {3.33f, 0.00f,  25,  0},
};

uint8_t power::calcLevel(float v, const VoltSegment* map, size_t len)
{
  for (size_t i=0;i<len;++i) {
    const auto& s = map[i];
    if (v<=s.v_hi && v>=s.v_lo) {
      float t = (v-s.v_lo)/(s.v_hi-s.v_lo);
      return uint8_t(lroundf(s.p_lo + t*(s.p_hi-s.p_lo)));
    }
  }
  return v>=map[0].v_hi ? 100 : 0;
}

void power::drawGauge(LGFX_Sprite& gfx, int16_t x, int16_t y)
{
  uint8_t pct = M5.Power.getBatteryLevel();
  uint16_t col = pct<=20 ? COL_ERR : COL_ON;

  constexpr int bw=20,bh=10, tipW=2, tipH=bh/2;
  gfx.drawRect(x, y, bw, bh, col);
  gfx.fillRect(x+bw, y+(bh-tipH)/2, tipW, tipH, col);

  int fill = map(pct,0,100,0,bw-2);
  gfx.fillRect(x+1, y+1, fill, bh-2, col);

  gfx.setFont(&fonts::lgfxJapanGothic_16);
  gfx.setTextColor(col, TFT_BLACK);
  gfx.drawString(String(pct)+"%", x+bw+tipW+4, y-3);
  gfx.setTextColor(COL_ON);
}
