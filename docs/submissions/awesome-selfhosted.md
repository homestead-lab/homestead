# Awesome-Selfhosted — ineligible for the main list under Commons Clause

The main list requires free/open-source software. Apache 2.0 with Commons Clause 1.0 is source-available and does not meet that requirement. This licensing blocker supersedes the earlier age/category assessment below: waiting four months or choosing Self-hosting Solutions does not resolve it. Any non-free companion list would need a separate rules review.

Rechecked 3 October 2026 against the [contribution rules](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/CONTRIBUTING.md), [PR checklist](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/.github/PULL_REQUEST_TEMPLATE.md) and actual category listings. This supersedes the earlier categorical exclusion in this pack.

## AI policy: submission versus software

The contribution guide prohibits machine/LLM-generated contributions. The PR checklist requires a human submission. Neither document explicitly establishes a blanket ban on software developed with AI assistance. This does not establish that AI-assisted submission text is permitted: the guide does not define the boundary, and human review alone is not a stated exception. A human should write any eventual contribution themselves and clarify ambiguous policy with maintainers if needed. This pack is internal research, not text to paste into that contribution.

## Category assessment

Position the whole product as a **hyperconverged homelab platform** designed to make combined compute, storage and networking approachable at home, including cluster creation, node administration and platform upgrades. The dashboard and Portal are individual features; a category choice should reflect the full scope.

- **Personal Dashboards:** the [category](https://awesome-selfhosted.net/tags/personal-dashboards.html) covers information and application access, with examples such as Homepage, Homarr, Heimdall and Dashy. Homestead's Portal overlaps with that purpose. However, its principal functions include deployment, virtualization and cluster administration, so the whole product's fit is uncertain. This is our assessment, not a maintainer ruling.
- **Self-hosting Solutions:** the [category](https://awesome-selfhosted.net/tags/self-hosting-solutions.html) covers installing, managing and configuring self-hosted applications. It includes Tipi, HomelabOS, OpenMediaVault and Nirvati, the last of which lists Kubernetes as a platform. Homestead's guided installer, App Store, shares and application-management workflows give it a plausible fit here. Kubernetes alone is not a disqualifier.

The general rules still direct generic container, deployment and virtualization tools to Awesome Sysadmin and exclude generic application platforms. The existing Self-hosting Solutions entries make the boundary less absolute than this pack originally suggested. Maintainers would need to decide whether Homestead is a home-server solution within scope or primarily a generic infrastructure tool. Present the complete product honestly when seeking that decision.

## Timing and next step

The PR checklist requires the first release to be more than four months old. Homestead's evidence currently does not establish that age requirement. Establish the original first public release date and revisit once it qualifies; repository creation and retained release history are not interchangeable with that date.

Recommended status: **do not submit to the main FOSS list under the current licence**. If the licence changes again, revisit age, authorship and category requirements. No inquiry or submission has been sent.

The list's source is `awesome-selfhosted/awesome-selfhosted-data`, with records in `software/*.yml`; the main Awesome-Selfhosted README is generated.

See also [Awesome Sysadmin's separate eligibility requirements](awesome-sysadmin.md). Homestead is not automatically eligible there either.
