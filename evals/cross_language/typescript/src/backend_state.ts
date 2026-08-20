export interface BackendState {
  taskState: "Ready" | "Running";
  localAddress: string;
  listenerPid: number;
  ownerPid: number;
  health: "ok" | "failed";
}

export function isBackendReady(state: BackendState): boolean {
  return (
    state.taskState === "Running" &&
    state.localAddress === "127.0.0.1" &&
    state.health === "ok"
  );
}
