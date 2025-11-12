#include "orientation.h"

#include <cassert>
#include <cmath>
#include <iostream>
// Простенькая тестовая обёртка для проверки класса интегратора.
// Используем assert и подробные сообщения в stdout, чтобы в случае падения
// было проще понять, что пошло не так.

using Orientation::YawIntegrator;

static void test_reset_and_idle() {
  YawIntegrator yaw;
  yaw.reset(15.0f, 100u);
  assert(yaw.yaw_deg() == 15.0f);
  assert(yaw.last_update_ms() == 100u);
  assert(yaw.is_idle());
  assert(!yaw.is_stuck(150u, 0.1f, 50u));
}

static void test_integration_positive() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(true, 0.1f, 0.5f, 50u);
  yaw.update(90.0f, 0u);   // Первое обновление, только инициализация.
  yaw.update(90.0f, 1000u); // 1 секунда -> +90°
  assert(std::fabs(yaw.yaw_deg() - 90.0f) < 1e-4f);
  assert(std::fabs(yaw.last_rate_dps() - 90.0f) < 1e-4f);
  assert(std::fabs(yaw.last_raw_rate_dps() - 90.0f) < 1e-4f);
  assert(!yaw.is_idle());
}

static void test_integration_negative() {
  YawIntegrator yaw;
  yaw.reset(10.0f, 0u);
  yaw.configure_bias(true, 0.1f, 0.5f, 50u);
  yaw.update(-45.0f, 0u);
  yaw.update(-45.0f, 2000u); // 2 секунды -> -90°
  assert(std::fabs(yaw.yaw_deg() - (-80.0f)) < 1e-4f);
  assert(std::fabs(yaw.last_rate_dps() - (-45.0f)) < 1e-4f);
  assert(std::fabs(yaw.last_raw_rate_dps() - (-45.0f)) < 1e-4f);
}

static void test_stuck_detection() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(false, 0.0f, 0.0f, 0u); // отключаем, чтобы не мешало тесту
  yaw.update(0.2f, 0u);
  yaw.update(0.05f, 100u);
  // Скорость ниже порога 0.1 уже 100 мс -> считается застрявшим.
  assert(yaw.is_stuck(250u, 0.1f, 100u));
}

static void test_stuck_waits_for_timeout() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(false, 0.0f, 0.0f, 0u);
  yaw.update(0.01f, 0u);
  yaw.update(0.02f, 80u);
  // Подробный комментарий: до истечения заданного тайм-аута метод должен
  // возвращать false, даже если скорости малы. Это важно для корректного
  // старта движений после сброса таймера прогресса.
  assert(!yaw.is_stuck(150u, 0.5f, 300u));
}

static void test_time_backwards_is_safe() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(true, 0.05f, 0.3f, 80u);
  yaw.update(90.0f, 0u);
  yaw.update(90.0f, 1000u);
  // Передаём «старое» время — интегратор должен проигнорировать dt.
  yaw.update(45.0f, 900u);
  assert(std::fabs(yaw.yaw_deg() - 90.0f) < 1e-4f);
  assert(std::fabs(yaw.last_rate_dps() - 45.0f) < 1e-4f);
}

static void test_bias_compensation_at_rest() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(true, 0.12f, 3.5f, 60u);

  // Имитация 5 секунд стоянки с постоянным смещением -1.5°/с.
  float timeMs = 0.0f;
  for (int i = 0; i < 250; ++i) {
    yaw.update(-1.5f, static_cast<uint32_t>(timeMs));
    timeMs += 20.0f;
  }

  // Ожидаем, что угол остался близко к нулю, а смещение приблизилось к -1.5.
  const float yawAfter = yaw.yaw_deg();
  const float biasEst  = yaw.bias_dps();
  assert(std::fabs(yawAfter) < 0.8f);
  assert(std::fabs(biasEst - (-1.5f)) < 0.2f);
}

static void test_bias_does_not_kill_real_rotation() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(true, 0.05f, 3.0f, 80u);

  // Сначала дадим немного времени на оценку смещения.
  uint32_t ts = 0u;
  for (int i = 0; i < 100; ++i) {
    yaw.update(1.0f, ts);
    ts += 10u;
  }

  // Теперь добавим реальное вращение: +90° за 1 секунду, но с тем же bias.
  const float true_rate = 90.0f;
  const float bias = 1.0f;
  for (int i = 0; i < 100; ++i) {
    yaw.update(true_rate + bias, ts);
    ts += 10u;
  }

  const float yawAfterTurn  = yaw.yaw_deg();
  const float rateAfterTurn = yaw.last_rate_dps();
  const float rawRate       = yaw.last_raw_rate_dps();
  assert(std::fabs(yawAfterTurn - 90.0f) < 1.0f);
  assert(std::fabs(rateAfterTurn - true_rate) < 1.0f);
  assert(std::fabs(rawRate - (true_rate + bias)) < 1e-3f);
}

static void test_wrapped_vs_unwrapped_angles() {
  YawIntegrator yaw;
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(false, 0.0f, 0.0f, 0u);

  // Эмулируем вращение по часовой стрелке (положительное направление) два полных оборота.
  yaw.update(360.0f, 0u);    // Первое измерение используется как отправная точка.
  yaw.update(360.0f, 1000u); // +360° за 1 секунду.
  yaw.update(360.0f, 2000u); // Ещё +360°.

  const float unwrappedPositive = yaw.yaw_deg_unwrapped();
  const float wrappedPositive   = yaw.yaw_deg();
  assert(std::fabs(unwrappedPositive - 720.0f) < 1e-3f);
  assert(wrappedPositive >= -180.0f && wrappedPositive < 180.0f);

  // Теперь проверим вращение в обратную сторону на большую величину.
  yaw.reset(0.0f, 0u);
  yaw.configure_bias(false, 0.0f, 0.0f, 0u);
  yaw.update(-270.0f, 0u);
  yaw.update(-270.0f, 1500u); // 1.5 секунды -> -405°.

  const float unwrappedNegative = yaw.yaw_deg_unwrapped();
  const float wrappedNegative   = yaw.yaw_deg();
  assert(std::fabs(unwrappedNegative + 405.0f) < 1e-3f);
  assert(wrappedNegative >= -180.0f && wrappedNegative < 180.0f);
}

static void test_bias_config_clamps_values() {
  YawIntegrator yaw;
  yaw.reset();
  // Передаём заведомо некорректные параметры, чтобы убедиться в ручном
  // ограничении без std::clamp.
  yaw.configure_bias(true, -1.0f, -5.0f, 10u);
  assert(std::fabs(yaw.bias_dps()) < 1e-6f);
  yaw.update(0.0f, 0u);
  // bias_alpha_ должен быть зажат в 0..1, а порог не может быть отрицательным.
  yaw.configure_bias(true, 2.0f, -3.0f, 10u);
  yaw.update(0.0f, 10u);
  // Просто проверяем отсутствие аварий и корректную работу фильтра с крайними
  // значениями — если бы clamp не сработал, фильтр мог бы сделать угол NaN.
  assert(std::isfinite(yaw.yaw_deg()));
}

int main() {
  test_reset_and_idle();
  test_integration_positive();
  test_integration_negative();
  test_stuck_detection();
  test_stuck_waits_for_timeout();
  test_time_backwards_is_safe();
  test_bias_compensation_at_rest();
  test_bias_does_not_kill_real_rotation();
  test_wrapped_vs_unwrapped_angles();
  test_bias_config_clamps_values();

  std::cout << "All orientation tests passed" << std::endl;
  return 0;
}

