#pragma once
#include <M5Unified.h>
#include "Config.h"
#include "DisplayAdapter.h"   // даёт COL_ON / COL_OFF

namespace power {

struct VoltSegment { float v_hi, v_lo; uint8_t p_hi, p_lo; };

uint8_t  calcLevel(float v, const VoltSegment* map, size_t len);
void     drawGauge(LGFX_Sprite& gfx, int16_t x, int16_t y);

} // namespace power
