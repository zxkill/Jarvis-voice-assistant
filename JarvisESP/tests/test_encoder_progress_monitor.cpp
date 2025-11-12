#include "motion_math.h"

#include <cassert>
#include <cstdint>
#include <cstdio>

using Motion::EncoderProgressMonitor;

int main() {
  EncoderProgressMonitor monitor;
  const uint32_t start = 1'000u;
  monitor.reset(start);

  // Проверяем, что после сброса значения равны нулю и таймер установлен.
  assert(monitor.last_left_ticks == 0);
  assert(monitor.last_right_ticks == 0);
  assert(monitor.last_progress_ms == start);

  // Без изменения тиков update возвращает false и не сдвигает таймер.
  const bool noChange = monitor.update(0, 0, start + 100u);
  assert(!noChange);
  assert(monitor.last_progress_ms == start);

  // Любое изменение тиков должно обновлять таймер и возвращать true.
  const bool changed = monitor.update(5, 0, start + 200u);
  assert(changed);
  assert(monitor.last_left_ticks == 5);
  assert(monitor.last_right_ticks == 0);
  assert(monitor.last_progress_ms == start + 200u);

  // Проверяем таймаут: если прошло больше лимита, получаем true.
  const bool timedOut = monitor.is_timed_out(start + 200u + 501u, 500u);
  assert(timedOut);

  // Но если прогресс обновился совсем недавно, таймаут не сработает.
  monitor.update(5, 1, start + 600u);
  const bool timedOutAfterProgress = monitor.is_timed_out(start + 950u, 500u);
  assert(!timedOutAfterProgress);

  std::puts("EncoderProgressMonitor tests passed");
  return 0;
}
