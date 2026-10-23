import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// All knobs come from the campaign; run independently with k6 too.
const base = __ENV.TARGET_URL || 'http://gateway:8080';
const rate = Number(__ENV.RATE || 100);
const duration = __ENV.DURATION || '20s';
const hotPercent = Number(__ENV.HOT_PERCENT || 0.90);
const writePercent = Number(__ENV.WRITE_PERCENT || 0);
const keyspace = Math.max(2, Math.min(10000, Number(__ENV.KEYSPACE || 100)));
const preVUs = Number(__ENV.PRE_VUS || 120);
const maxVUs = Number(__ENV.MAX_VUS || 500);

const cacheHitRate = new Rate('client_cache_hit_ratio');
const readLatency = new Trend('read_latency_ms', true);
const writeLatency = new Trend('write_latency_ms', true);
const failedReads = new Counter('failed_reads');

export const options = {
  // Bound cardinality: individual product URLs and VU/iteration IDs are omitted.
  systemTags: ['status', 'method', 'scenario', 'check', 'expected_response'],
  tags: { strategy: __ENV.CACHE_STRATEGY || 'unknown' },
  scenarios: {
    fixed_arrivals: {
      executor: 'constant-arrival-rate',
      rate: rate,
      timeUnit: '1s',
      duration: duration,
      preAllocatedVUs: preVUs,
      maxVUs: maxVUs,
      gracefulStop: '4s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  // Thresholds intentionally not enforced: overload/failure experiments must report,
  // not abort. The campaign compares reported values with documented decision limits.
};

function chooseEvent() {
  return Math.random() < hotPercent ? 1 : 2 + Math.floor(Math.random() * (keyspace - 1));
}

export default function () {
  const eventId = chooseEvent();
  if (Math.random() < writePercent) {
    const res = http.post(`${base}/products/${eventId}/activity`,
      JSON.stringify({kind: 'favorite'}),
      {headers: {'Content-Type': 'application/json'}, tags: {operation: 'write'}});
    check(res, {'write status 200': (r) => r.status === 200});
    writeLatency.add(res.timings.duration);
  } else {
    const res = http.get(`${base}/products/${eventId}/snapshot`, {tags: {operation: 'read'}});
    check(res, {'read status 200': (r) => r.status === 200});
    readLatency.add(res.timings.duration);
    if (res.status !== 200) failedReads.add(1);
    const src = res.headers['X-Cache-Source'] || '';
    cacheHitRate.add(src === 'cache' || src === 'cache_wait');
  }
}
