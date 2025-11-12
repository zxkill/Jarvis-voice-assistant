#pragma once
#include <Arduino.h>

// До двух одноканальных энкодеров (по одному входу каждый).
// Дребезг/помехи режем по минимальной длительности импульса.

enum EncId : uint8_t { ENC1 = 0, ENC2 = 1 };

void enc_init(EncId id, uint8_t pinSignal, bool externalPullup = true);
void enc_set_min_pulse_us(uint32_t us); // общий порог для обоих, по умолч. ~120 мкс

long enc_peek(EncId id);           // текущее накопленное значение (не обнуляет)
long enc_get_and_clear(EncId id);  // атомарно взять дельту и обнулить
