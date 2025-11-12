#include "encoder_dual.h"

static volatile long s_ticks[2] = {0,0};
static uint8_t s_pin[2] = {0,0};
static volatile uint32_t s_last_isr_us[2] = {0,0};
static uint32_t s_min_pulse_us = 120;

static void IRAM_ATTR isr0() {
  const uint32_t now = micros();
  if ((now - s_last_isr_us[0]) < s_min_pulse_us) return;
  s_last_isr_us[0] = now;
  s_ticks[0]++;
}
static void IRAM_ATTR isr1() {
  const uint32_t now = micros();
  if ((now - s_last_isr_us[1]) < s_min_pulse_us) return;
  s_last_isr_us[1] = now;
  s_ticks[1]++;
}

void enc_init(EncId id, uint8_t pinSignal, bool externalPullup) {
  s_pin[id] = pinSignal;
  pinMode(s_pin[id], externalPullup ? INPUT : INPUT_PULLUP);

  if (id == ENC1) attachInterrupt(digitalPinToInterrupt(s_pin[id]), isr0, FALLING);
  else            attachInterrupt(digitalPinToInterrupt(s_pin[id]), isr1, FALLING);

  delay(5);
  Serial.printf("[ENC%u] init: pin=%u level=%d minPulse=%uus\n",
                (unsigned)id+1, s_pin[id], digitalRead(s_pin[id]), s_min_pulse_us);
}

void enc_set_min_pulse_us(uint32_t us) { s_min_pulse_us = us; }

long enc_peek(EncId id) {
  noInterrupts();
  long v = s_ticks[id];
  interrupts();
  return v;
}

long enc_get_and_clear(EncId id) {
  noInterrupts();
  long v = s_ticks[id];
  s_ticks[id] = 0;
  interrupts();
  return v;
}
