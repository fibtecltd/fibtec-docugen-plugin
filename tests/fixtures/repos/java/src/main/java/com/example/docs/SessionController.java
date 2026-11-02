package com.example.docs;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/sessions")
public class SessionController {
    private final SessionService service;

    public SessionController(SessionService service) { this.service = service; }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Session create(@RequestParam String tenant, @RequestParam String repo) {
        return service.submit(tenant, repo);
    }

    @GetMapping("/{id}")
    public String get(@PathVariable long id) { return "session " + id; }
}
