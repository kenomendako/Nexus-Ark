// 長い生成を許容しつつ、通信が終わらない要求のローカル待機を有限にする。
export const PROVIDER_TOTAL_TIMEOUT_MS = 25 * 60 * 1000;
export const REQUEST_EXPIRY_MS = 30 * 60 * 1000;
export const REQUEST_EXPIRY_CRON = "*/5 * * * *";

export function createProviderDeadline(
  fetcher: typeof fetch,
  timeoutMs = PROVIDER_TOTAL_TIMEOUT_MS,
  requestSignal?: AbortSignal,
) {
  const abortController = new AbortController();
  let expired = false;
  let requestAborted = false;
  let rejectDeadline!: (reason: Error) => void;
  const deadline = new Promise<never>((_resolve, reject) => { rejectDeadline = reject; });
  // DB処理中など、raceで監視していない時点に期限が来ても未処理rejectionにしない。
  void deadline.catch(() => {});
  let removeRequestAbortListener: (() => void) | null = null;
  const timer = setTimeout(() => {
    expired = true;
    abortController.abort();
    rejectDeadline(new Error("provider_deadline_exceeded"));
  }, timeoutMs);
  const abortForRequest = () => {
    if (requestAborted || expired) return;
    requestAborted = true;
    clearTimeout(timer);
    abortController.abort();
    rejectDeadline(new Error("request_aborted"));
  };
  if (requestSignal) {
    const listener = () => abortForRequest();
    if (requestSignal.aborted) {
      listener();
    } else {
      requestSignal.addEventListener("abort", listener, { once: true });
      removeRequestAbortListener = () => requestSignal.removeEventListener("abort", listener);
    }
  }
  const assertActive = () => {
    if (expired) throw new Error("provider_deadline_exceeded");
    if (requestAborted) throw new Error("request_aborted");
  };
  const stop = () => {
    clearTimeout(timer);
    removeRequestAbortListener?.();
    removeRequestAbortListener = null;
  };
  return {
    assertActive,
    wait: <T>(operation: Promise<T>) => {
      assertActive();
      return Promise.race([operation, deadline]);
    },
    fetch: ((input, init) => {
      assertActive();
      return Promise.race([fetcher(input, { ...init, signal: abortController.signal }), deadline]);
    }) as typeof fetch,
    read: <T>(reader: ReadableStreamDefaultReader<T>) => {
      assertActive();
      return Promise.race([reader.read(), deadline]);
    },
    stop,
    abort: () => abortController.abort(),
    expired: () => expired,
    aborted: () => requestAborted,
  };
}
