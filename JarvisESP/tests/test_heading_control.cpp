#include "heading_control.h"

#include <cassert>
#include <cmath>
#include <iostream>

using Motion::YawIntegralAccumulator;

static void test_accumulates_and_clamps() {
  YawIntegralAccumulator acc;
  acc.set_limit(5.0f);
  acc.reset(1000u);

  assert(std::fabs(acc.value()) < 1e-6f);

  float v1 = acc.update(2.0f, 1100u);
  float v2 = acc.update(2.0f, 1200u);
  float v3 = acc.update(100.0f, 2200u);
  float v4 = acc.update(-50.0f, 3200u);

  assert(std::fabs(v1 - 0.2f) < 1e-5f);
  assert(std::fabs(v2 - 0.4f) < 1e-5f);
  assert(std::fabs(v3 - 5.0f) < 1e-5f);
  assert(std::fabs(v4 - (-5.0f)) < 1e-5f);
}

static void test_negative_limit_treated_as_absolute() {
  YawIntegralAccumulator acc;
  acc.set_limit(-3.0f);
  acc.reset(0u);

  float v1 = acc.update(10.0f, 1000u);
  float v2 = acc.update(-10.0f, 2000u);

  assert(std::fabs(v1 - 3.0f) < 1e-5f);
  assert(std::fabs(v2 - (-3.0f)) < 1e-5f);
}

int main() {
  std::cout << "Running YawIntegralAccumulator tests..." << std::endl;
  test_accumulates_and_clamps();
  test_negative_limit_treated_as_absolute();
  std::cout << "All heading control tests passed" << std::endl;
  return 0;
}

