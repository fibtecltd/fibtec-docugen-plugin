package com.example.docs;

import java.time.Instant;

public class Session implements Auditable {
    private final long id;
    private final String tenant;
    private SessionState state = SessionState.QUEUED;
    private final Instant createdAt = Instant.now();

    public Session(long id, String tenant) {
        this.id = id;
        this.tenant = tenant;
    }

    public void finish(boolean ok) {
        this.state = ok ? SessionState.DONE : SessionState.FAILED;
    }

    public long getId() { return id; }
    public SessionState getState() { return state; }
    @Override public Instant getCreatedAt() { return createdAt; }
}
