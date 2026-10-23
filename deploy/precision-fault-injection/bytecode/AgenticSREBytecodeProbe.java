public final class AgenticSREBytecodeProbe {
    private static volatile long invocationCount = 0;

    private AgenticSREBytecodeProbe() {
    }

    public static String stableOperation() {
        invocationCount += 1;
        return "OK";
    }

    public static void main(String[] args) throws InterruptedException {
        long iteration = 0;
        while (true) {
            iteration += 1;
            long startedAt = System.nanoTime();
            try {
                String value = stableOperation();
                long latencyMillis = (System.nanoTime() - startedAt) / 1_000_000;
                System.out.printf(
                    "result=ok iteration=%d latency_ms=%d value=%s invocations=%d%n",
                    iteration,
                    latencyMillis,
                    value,
                    invocationCount
                );
            } catch (Throwable error) {
                long latencyMillis = (System.nanoTime() - startedAt) / 1_000_000;
                System.out.printf(
                    "result=fault iteration=%d latency_ms=%d error=%s message=%s%n",
                    iteration,
                    latencyMillis,
                    error.getClass().getName(),
                    error.getMessage()
                );
            }
            Thread.sleep(250);
        }
    }
}
