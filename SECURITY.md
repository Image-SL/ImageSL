# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a
vulnerability** button under the Security tab, rather than opening a public
issue.

Include what you did, what happened, what you expected, and the version
affected (shown in Settings and in every CSV export). A proof of concept is
welcome but not required.

Expect an acknowledgement within a few days. Please give a reasonable window
for a fix before public disclosure.

## Scope

In scope:

* The desktop application and its update mechanism
* The server in `server/`, including the API and the download endpoints
* The build and release pipeline in `.github/workflows/`

Out of scope:

* Vulnerabilities in third-party dependencies with no ImageSL-specific impact
  (report those upstream)
* Findings that require an attacker who already has local administrator rights
* Missing hardening headers with no demonstrated impact

## Update integrity

Updates are verified before anything is executed. The site publishes a SHA-256
digest over HTTPS, the downloaded installer must match it, and the file is
re-hashed immediately before it is run. Partial updates apply the same rule to
every individual file. An update that cannot be verified is refused rather
than run, and paths supplied by an update manifest are rejected if they escape
the application directory.

If you find a way around any of that, it is in scope and we want to hear.
