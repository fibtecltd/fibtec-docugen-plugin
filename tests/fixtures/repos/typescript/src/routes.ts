import express from "express";
import { QuotaExceeded, submit } from "./service";
import { Tenant } from "./domain/session";

export const router = express.Router();

router.post("/v1/sessions", (req, res) => {
  try {
    const s = submit(new Tenant(1, "acme"), String(req.query.repo));
    res.status(202).json({ id: s.id, state: s.state });
  } catch (e) {
    if (e instanceof QuotaExceeded) return res.status(402).json({ detail: "no credit" });
    throw e;
  }
});

router.get("/v1/sessions/:id", (req, res) => {
  res.json({ id: Number(req.params.id) });
});
