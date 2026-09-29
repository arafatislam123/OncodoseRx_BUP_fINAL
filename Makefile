SIM ?= http://localhost:8000
API ?= http://localhost:8080
PY ?= python
JSON = -H 'Content-Type: application/json'

# current simulator tick, read through /admin-free health endpoint
NOW = $$(curl -s $(SIM)/v1/health | $(PY) -c "import sys,json;print(json.load(sys.stdin)['simulation']['tick'])")

.PHONY: up down logs run pause step reset probes spike crisis chaos-errors chaos-sse chaos-latency chaos-stale chaos-clear \
        test test-integration test-live test-crash compare-policies replay loadtest loadtest-soak lint

## --- stack -------------------------------------------------------------
up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100 agent api

## --- simulator clock ---------------------------------------------------
run:
	curl -s -X POST $(SIM)/admin/run; echo

pause:
	curl -s -X POST $(SIM)/admin/pause; echo

step:
	curl -s -X POST $(SIM)/admin/step; echo

reset:
	curl -s -X POST $(SIM)/admin/reset; echo

probes:
	$(PY) -m tools.probes.run --sim $(SIM)

## --- demo events -------------------------------------------------------
spike:
	curl -s -X POST $(SIM)/admin/events $(JSON) -d "{\"type\":\"demand_spike\",\"start_tick\":$$(( $(NOW) + 12 )),\"duration_ticks\":24,\"parameters\":{\"region_ids\":[\"region-dhaka\"],\"multiplier\":1.8}}"; echo

crisis:
	curl -s -X POST $(SIM)/admin/events $(JSON) -d "{\"type\":\"route_disruption\",\"start_tick\":$$(( $(NOW) + 1 )),\"duration_ticks\":24,\"parameters\":{\"route_ids\":[\"route-patiya-coxsbazar\"]}}"; echo
	curl -s -X POST $(SIM)/admin/events $(JSON) -d "{\"type\":\"supply_shortfall\",\"start_tick\":$$(( $(NOW) + 1 )),\"duration_ticks\":1,\"parameters\":{\"depot_ids\":[\"depot-gazipur\"],\"factor\":0.5}}"; echo

chaos-errors:
	curl -s -X POST $(SIM)/admin/faults $(JSON) -d '{"type":"error_rate","duration_seconds":45,"parameters":{"rate":0.4}}'; echo

chaos-sse:
	curl -s -X POST $(SIM)/admin/faults $(JSON) -d '{"type":"stream_disconnect","duration_seconds":30}'; echo

chaos-latency:
	curl -s -X POST $(SIM)/admin/faults $(JSON) -d '{"type":"latency","duration_seconds":30,"parameters":{"delay_ms":500}}'; echo

chaos-stale:
	curl -s -X POST $(SIM)/admin/faults $(JSON) -d '{"type":"stale_data","duration_seconds":60}'; echo

chaos-clear:
	curl -s -X POST $(SIM)/admin/faults/clear; echo

## --- tests -------------------------------------------------------------
lint:
	ruff check agent api harness tools tests
	cd web && npm run lint

test:
	$(PY) -m pytest tests/unit tests/contract -q
	cd web && npm test --silent

test-integration:
	$(PY) -m pytest tests/integration -q

test-live:
	$(PY) -m harness.live --sim $(SIM)

test-crash:
	$(PY) -m harness.crash --sim $(SIM)

compare-policies:
	$(PY) -m harness.compare_policies --sim $(SIM) --out docs/results/policy_comparison.md

replay:
	$(PY) -m tools.replay --decision $(DECISION)

## --- load tests --------------------------------------------------------
loadtest:
	k6 run -e API=$(API) loadtest/l1_read.js --summary-export loadtest/results/l1.json
	k6 run -e API=$(API) loadtest/l2_whatif.js --summary-export loadtest/results/l2.json
	$(PY) loadtest/summarize.py loadtest/results > loadtest/results/summary.md

loadtest-soak:
	k6 run -e API=$(API) loadtest/l3_soak.js --summary-export loadtest/results/l3.json
