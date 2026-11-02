export enum SessionState {
  Queued = "queued",
  Running = "running",
  Done = "done",
  Failed = "failed",
}

export interface Auditable {
  createdAt: Date;
}

export class Tenant {
  constructor(public readonly id: number, public name: string) {}
}

export class Session implements Auditable {
  state: SessionState = SessionState.Queued;
  createdAt = new Date();

  constructor(public readonly id: number, public readonly tenant: Tenant) {}

  finish(ok: boolean): void {
    this.state = ok ? SessionState.Done : SessionState.Failed;
  }
}
