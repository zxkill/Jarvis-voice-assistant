#include "remote_control.h"

#include <algorithm>
#include <cassert>
#include <iostream>

namespace {

void test_single_command_roundtrip() {
  RemoteControl::clear_commands();

  RemoteControl::Command cmd{};
  cmd.action = RemoteControl::Action::Move;
  cmd.direction = RemoteControl::Direction::Backward;
  cmd.value = 1.25f;
  cmd.duty = 512;

  assert(RemoteControl::push_command(cmd));

  RemoteControl::Command fetched{};
  assert(RemoteControl::fetch_command(fetched));
  assert(fetched.action == RemoteControl::Action::Move);
  assert(fetched.direction == RemoteControl::Direction::Backward);
  assert(fetched.value > 1.24f && fetched.value < 1.26f);
  assert(fetched.duty == 512);
  assert(RemoteControl::is_busy());
  assert(!RemoteControl::telemetry_updates_allowed());
  RemoteControl::notify_command_complete();
  assert(!RemoteControl::is_busy());
  assert(RemoteControl::telemetry_updates_allowed());
  assert(!RemoteControl::fetch_command(fetched));
}

void test_queue_rejects_second_command() {
  RemoteControl::clear_commands();

  RemoteControl::Command first{};
  first.action = RemoteControl::Action::Rotate;
  first.direction = RemoteControl::Direction::Forward;
  first.value = 90.0f;

  RemoteControl::Command second = first;
  second.direction = RemoteControl::Direction::Backward;

  assert(RemoteControl::push_command(first));
  assert(!RemoteControl::push_command(second));

  RemoteControl::Command fetched{};
  assert(RemoteControl::fetch_command(fetched));
  assert(fetched.direction == RemoteControl::Direction::Forward);
  assert(RemoteControl::is_busy());
  assert(!RemoteControl::telemetry_updates_allowed());
  assert(!RemoteControl::push_command(second));
  RemoteControl::notify_command_complete();
  assert(RemoteControl::push_command(second));
}

void test_clear_commands_resets_queue() {
  RemoteControl::clear_commands();

  RemoteControl::Command cmd{};
  cmd.action = RemoteControl::Action::EmergencyStop;
  RemoteControl::push_command(cmd);

  RemoteControl::clear_commands();

  RemoteControl::Command fetched{};
  assert(!RemoteControl::fetch_command(fetched));
}

void test_emergency_stop_breaks_through() {
  RemoteControl::clear_commands();

  RemoteControl::Command move{};
  move.action = RemoteControl::Action::Move;
  move.value = 0.4f;
  assert(RemoteControl::push_command(move));

  RemoteControl::Command fetched{};
  assert(RemoteControl::fetch_command(fetched));
  assert(RemoteControl::is_busy());

  RemoteControl::Command stop{};
  stop.action = RemoteControl::Action::EmergencyStop;
  assert(RemoteControl::push_command(stop));

  RemoteControl::Command stopFetched{};
  assert(RemoteControl::fetch_command(stopFetched));
  assert(stopFetched.action == RemoteControl::Action::EmergencyStop);
  assert(!RemoteControl::is_busy());
  assert(RemoteControl::telemetry_updates_allowed());
}

void test_audio_stream_config_api() {
  RemoteControl::AudioStreamConfig cfg{};
  cfg.endpoint = "wss://example.org/audio";
  cfg.authHeader = "Bearer token";
  cfg.subprotocol = "robot-stream";
  cfg.handshakeTimeoutMs = 4321;
  cfg.reconnectIntervalMs = 2222;
  cfg.pingIntervalMs = 7777;

  RemoteControl::set_audio_stream_config(cfg);

  const auto applied = RemoteControl::audio_stream_config();
  assert(applied.endpoint == cfg.endpoint);
  assert(applied.authHeader == cfg.authHeader);
  assert(applied.subprotocol == cfg.subprotocol);
  assert(applied.handshakeTimeoutMs == cfg.handshakeTimeoutMs);
  assert(applied.reconnectIntervalMs == cfg.reconnectIntervalMs);
  assert(applied.pingIntervalMs == cfg.pingIntervalMs);

  const auto stats = RemoteControl::audio_stream_stats();
  assert(stats.framesSent == 0);
  assert(stats.framesFailed == 0);
  assert(stats.nextSequence == 1);
  assert(!stats.lastAttemptOk);
  assert(stats.lastError.empty());
  assert(stats.bytesSent == 0);
  assert(!stats.wsConnected);
  assert(stats.wsReconnects == 0);
  assert(stats.queueDepth == 0);
  assert(stats.queueHighWatermark == 0);
  assert(stats.queueDrops == 0);
  assert(stats.queueStalls == 0);
  assert(stats.wsOfflineDrops == 0);
  assert(stats.wsTimeouts == 0);

  RemoteControl::AudioStreamConfig disabled{};
  disabled.endpoint.clear();
  RemoteControl::set_audio_stream_config(disabled);
  const auto disabledStats = RemoteControl::audio_stream_stats();
  assert(disabledStats.lastError == "disabled");
  assert(!disabledStats.wsConnected);
  assert(disabledStats.queueDepth == 0);
  assert(disabledStats.queueHighWatermark == 0);
  assert(disabledStats.queueDrops == 0);
  assert(disabledStats.queueStalls == 0);
  assert(disabledStats.wsOfflineDrops == 0);
  assert(disabledStats.wsTimeouts == 0);
}

void test_audio_stream_summary_formatting() {
  RemoteControl::AudioStreamConfig cfg{};
  RemoteControl::AudioStreamStats stats{};
  RemoteControl::Diagnostics diag{};

  // Без endpoint карточка должна явно сообщать об отключённом сервисе.
  const auto disabledSummary = RemoteControl::detail::build_audio_stream_summary(cfg, stats, diag);
  assert(disabledSummary.size() == 1);
  assert(disabledSummary[0] == "отключено");

  // Заполняем метрики аудиопотока, чтобы проверить перенос всех информативных меток.
  cfg.endpoint = "wss://speech.local/ws";
  diag.audioStreamReady = true;
  stats.framesSent = 42;
  stats.framesFailed = 3;
  stats.queueDepth = 2;
  stats.queueHighWatermark = 6;
  stats.queueDrops = 1;
  stats.wsOfflineDrops = 2;
  stats.queueStalls = 1;
  stats.wsConnected = true;
  stats.wsReconnects = 5;
  stats.wsTimeouts = 1;
  stats.bytesSent = 2048;
  stats.lastDurationMs = 18;
  stats.lastError = "late-pong";

  const auto activeSummary = RemoteControl::detail::build_audio_stream_summary(cfg, stats, diag);
  assert(activeSummary.size() >= 9);
  assert(activeSummary.front() == "готов");
  assert(activeSummary.back() == cfg.endpoint);
  // Проверяем наличие ключевых меток, которые должны отрисовываться построчно.
  assert(std::find(activeSummary.begin(), activeSummary.end(), "drop:1") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "offline:2") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "stall:1") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "ws:on") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "timeout:1") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "bytes:2048") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "18мс") != activeSummary.end());
  assert(std::find(activeSummary.begin(), activeSummary.end(), "late-pong") != activeSummary.end());
}

void test_telemetry_stats_defaults() {
  const auto telem = RemoteControl::telemetry_stream_stats();
  assert(telem.clientsConnected == 0);
  assert(telem.clientsMax == 0);
  assert(telem.messagesSent == 0);
  assert(telem.bytesSent == 0);
  assert(telem.duplicatesSkipped == 0);
  assert(telem.lastBroadcastMs == 0);
  assert(telem.lastError.empty());
}

void test_handshake_timeout_helper() {
  using RemoteControl::detail::handshake_timeout_elapsed;
  assert(!handshake_timeout_elapsed(0, 1000));
  assert(!handshake_timeout_elapsed(1000, 999));
  assert(handshake_timeout_elapsed(1000, 1001));
  assert(handshake_timeout_elapsed(1, 2));
}

} // namespace

int main() {
  test_single_command_roundtrip();
  test_queue_rejects_second_command();
  test_clear_commands_resets_queue();
  test_emergency_stop_breaks_through();
  test_audio_stream_config_api();
  test_audio_stream_summary_formatting();
  test_telemetry_stats_defaults();
  test_handshake_timeout_helper();

  std::cout << "Remote control queue tests passed" << std::endl;
  return 0;
}
