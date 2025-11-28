#include "remote_control.h"

#include <cassert>
#include <iostream>
#include <string>

int main() {
  RemoteControl::AudioStreamConfig cfg{};
  cfg.endpoint = "ws://example.com";
  RemoteControl::AudioStreamStats stats{};
  stats.wsConnected = false;
  stats.wsReconnects = 3;
  stats.lastError = "ws-offline";

  const std::string desc = RemoteControl::detail::describe_ws_offline_reason(
      cfg, stats, false, 5000, 3200);

  // Проверяем ключевые элементы диагностической строки, чтобы разработчик видел причину отвалов.
  assert(desc.find("wifi=down") != std::string::npos);
  assert(desc.find("cfg=ok") != std::string::npos);
  assert(desc.find("ws=off") != std::string::npos);
  assert(desc.find("reconns=3") != std::string::npos);
  assert(desc.find("lastErr=ws-offline") != std::string::npos);
  assert(desc.find("timeoutMs=5000") != std::string::npos);
  assert(desc.find("elapsedMs=3200") != std::string::npos);

  std::cout << "describe_ws_offline_reason ok: " << desc << std::endl;
  return 0;
}
