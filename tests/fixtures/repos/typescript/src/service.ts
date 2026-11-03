import { Session, Tenant } from "./domain/session";

export class QuotaExceeded extends Error {}

export function reserveCredit(tenant: Tenant, amount: number): void {
  if (amount > 100) throw new QuotaExceeded(tenant.name);
}

export function submit(tenant: Tenant, repo: string): Session {
  reserveCredit(tenant, 4);
  return new Session(Date.now(), tenant);
}
