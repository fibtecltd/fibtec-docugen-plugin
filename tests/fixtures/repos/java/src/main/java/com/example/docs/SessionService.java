package com.example.docs;

import org.springframework.stereotype.Service;

@Service
public class SessionService {
    public static class QuotaExceeded extends RuntimeException {
        public QuotaExceeded(String tenant) { super(tenant); }
    }

    void reserveCredit(String tenant, int amount) {
        if (amount > 100) throw new QuotaExceeded(tenant);
    }

    public Session submit(String tenant, String repo) {
        reserveCredit(tenant, 4);
        return new Session(System.currentTimeMillis(), tenant);
    }
}
