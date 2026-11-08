package main

import "testing"

func TestWatchdogUsesFullestPartition(t *testing.T) {
	stats := AgentStats{
		HDDTotal: 1100,
		HDDUsed:  300,
		HDDParts: []HDDPart{
			{Mount: "/", Total: 1000, Used: 200},
			{Mount: "/data", Total: 100, Used: 100},
		},
	}
	environment := watchdogEnvironment(ServerConfig{}, stats, true, false, 0, 0)
	if environment.HDDMaxPct != 100 || environment.HDDMaxMount != "/data" {
		t.Fatalf("watchdog did not pick the fullest partition: %#v", environment)
	}
	if environment.HDDTotal != 1100 || environment.HDDUsed != 300 {
		t.Fatalf("aggregate fields must stay untouched: %#v", environment)
	}
}

func TestWatchdogFallsBackToAggregateWithoutPartitions(t *testing.T) {
	environment := watchdogEnvironment(ServerConfig{}, AgentStats{HDDTotal: 100, HDDUsed: 91}, true, false, 0, 0)
	if environment.HDDMaxPct != 91 || environment.HDDMaxMount != "" {
		t.Fatalf("aggregate fallback changed: %#v", environment)
	}
}

func TestWatchdogIgnoresEmptyPartitions(t *testing.T) {
	stats := AgentStats{HDDParts: []HDDPart{{Mount: "/proc", Total: 0, Used: 0}}}
	environment := watchdogEnvironment(ServerConfig{}, stats, true, false, 0, 0)
	if environment.HDDMaxPct != 0 || environment.HDDMaxMount != "" {
		t.Fatalf("zero-sized partitions must be skipped: %#v", environment)
	}
}
