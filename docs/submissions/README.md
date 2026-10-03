# Homestead submission pack

Prepared 3 October 2026 for [wjcloudy/homestead](https://github.com/wjcloudy/homestead), refreshed against `e2302feb363ef55a0f3296eec36b9def6e106023` (2.8.300) and the subsequent licensing change in this branch. All submissions remain unsent.

Project maturity updated to **beta** on 3 October 2026. The copy below reflects that designation. Beta status does not change the first public release date or satisfy a directory's minimum-age requirement.

Lead with **hyperconvergence for home, made simple**. The product category is **hyperconverged homelab platform**: compute, storage and networking across ordinary machines, managed through one friendly interface. Explain the simplicity through guided installation, familiar app and volume workflows, and integrated update controls. Supporting copy should include cluster setup, node administration, platform upgrades, containers, VMs and backups. Use “dashboard” for the overview screen. Explain replication and failover where space permits, with their configuration requirements. Keep beta status visible in longer descriptions. See the [positioning and evidence](product-facts.md#positioning).

Homestead is now **source-available under Apache 2.0 with Commons Clause 1.0**. Use that full licence name in submissions; do not select plain Apache-2.0, MIT or open source. Historical MIT releases remain MIT. See the [licensing guide](../licensing.md). The ranking below reflects the new restriction; the main Awesome-Selfhosted and Awesome Sysadmin lists require free/open-source software.

## Suggested submission order

This is an editorial ranking based on audience fit, Homestead's current beta stage and the submission requirements checked on 3 October 2026. It is not a ranking by directory size or a prediction of traffic.

First, review the [GitHub About description, demo link and topics](github-metadata.md), so visitors arriving from any listing see consistent information.

| Order | Destination | Why it belongs here | Use this draft |
|---|---|---|---|
| 1 | **selfh.st — Self-Host Weekly / Project Launch** | Its form directs new projects here. Disclose the Commons Clause; this is a newsletter pitch, not a directory listing or confirmed acceptance. | [Project Launch](selfhst.md#project-launch-form) |
| 2 | **HomelabAddiction** | Relevant audience and free self-hosted core features. Disclose the source-available terms for editorial review. | [Directory suggestion email](homelabaddiction.md) |
| 3 | **Selfhost Tools** | Relevant self-hosting audience and DevOps category. Confirm that the editor accepts this licence. | [Form answers](selfhost-tools.md) |
| 4 | **selfhost.directory** | Server Management and DevOps fit; use the general contact route with explicit licence disclosure. | [Contact message](selfhost-directory.md) |
| 5 | **AlternativeTo** | Useful product comparisons. Choose a proprietary/source-available classification as the form permits, with free pricing; do not select open source. | [Listing and comparison copy](alternativeto.md) |

The first three form the initial batch. There is no need to wait for one editor's response before approaching the next destination; keep a record of each submission and avoid duplicate submissions to the same site.

### Revisit later

- **selfh.st/apps:** the strongest directory follow-up once the project is no longer considered newly launched. Recheck the editor's criteria; no fixed waiting period was published in the inspected form.
- **AwesomeHomelab:** strong audience fit, but its stated focus is open-source apps. Hold until maintainers explicitly accept Commons Clause software; the saved draft is conditional.
- **Awesome Sysadmin:** ineligible for its main FOSS list under the new licence. Age and professional-use requirements would also remain relevant if licensing changed again.
- **Awesome-Selfhosted:** ineligible for its main FOSS list under the new licence, regardless of category or age. Investigate a separately maintained non-free companion list only under its own rules; no such submission is prepared here. See the [assessment](awesome-selfhosted.md).

## Submission tracker

“Prepared” means the copy is available for review, not that a directory has accepted the project. Check for an existing listing immediately before sending. Dates, contact details and first-hand experience must come from the submitter.

| Destination | Status | Prepared artefact | Submission route | Submitted / result URL |
|---|---|---|---|---|
| AwesomeHomelab | Hold: source-available eligibility | [Conditional draft](awesome-homelab.md), [conditional YAML](awesome-homelab-entry.yaml) | [New issue](https://github.com/AwesomeHomelab/awesome-homelab/issues/new) | — |
| Selfhost Tools | Prepared | [Form answers](selfhost-tools.md) | [Paperform](https://selfhosttools-suggest.paperform.co/) | — |
| HomelabAddiction | Prepared | [Email and technical details](homelabaddiction.md) | [Contact instructions](https://homelabaddiction.com/contact/) | — |
| AlternativeTo | Prepared; account and form review needed | [Listing copy and comparisons](alternativeto.md) | [Suggest new application instructions](https://alternativeto.net/faq/#add-a-new-application) | — |
| selfhost.directory | Prepared; general contact route | [Contact message](selfhost-directory.md) | [Homepage footer: Get in touch](https://selfhost.directory/) | — |
| selfh.st/apps | Hold: new project | [Directory copy and Project Launch alternative](selfhst.md) | [Submission form](https://selfh.st/submit/) | — |
| Awesome Sysadmin | Ineligible: main list requires FOSS | [Eligibility notes](awesome-sysadmin.md) | [Data repository](https://github.com/awesome-foss/awesome-sysadmin-data) | — |
| Awesome-Selfhosted | Ineligible: main list requires FOSS | [Eligibility and category assessment](awesome-selfhosted.md) | [Contribution rules](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/CONTRIBUTING.md) | — |
| GitHub Topics / About | Prepared; metadata unchanged | [Suggested metadata](github-metadata.md) | Repository About settings | — |

## Shared material

- [Product facts, reusable short copy and evidence](product-facts.md).
- [Logos, screenshots and suggested captions](assets.md).
- [Feature comparison and sources](../comparison.md), with the table in the [project README](../../README.md#how-homestead-compares). Use scoped comparisons rather than implying every product is interchangeable.
- Each destination file distinguishes observed form fields and published rules from editorial recommendations. Where no hard length limit was visible, the stated length is our writing target, not a claimed site requirement.
- YAML files are candidate records for external lists, not Homestead deployment manifests. Do not apply them with Kubernetes tooling.

## Copy length check

Counts include spaces and punctuation. Only Awesome Sysadmin's limit below was verified as an upstream rule; the others are conservative editorial targets.

| Copy | Characters | Target / rule |
|---|---:|---|
| Shared compact description / AwesomeHomelab entry | 138 | At most 160, editorial target |
| Shared extended list description | 152 | Fewer than 250; does not imply Awesome Sysadmin eligibility |
| AlternativeTo short description | 138 | At most 160, editorial target |
| Proposed GitHub About description | 151 | Concise summary; no keyword list |

The AlternativeTo full description is 178 words. All local Markdown links were checked for an existing target. Candidate YAML fields were compared with the upstream examples; upstream lint/build checks remain part of preparing an actual list PR.

## Before sending

1. Review the copy against the then-current release and rules. This pack is AI-assisted; it does not establish first-hand usage or eligibility.
2. Supply your chosen public name and reply address only where requested. On selfh.st, answer its question about AI-assisted development accurately; drafting this pack alone does not determine the answer.
3. Check for duplicate listings and prior submissions. AwesomeHomelab's infrastructure category and issue/PR searches contained no Homestead entry on the preparation date; other directories have not had an exhaustive duplicate audit.
4. Use the existing logo and release screenshots. The demo and screenshots use sample data. Confirm the download links still work before attaching images.
5. Record the submission date and resulting URL in the table above. No accounts, repository settings, issues, pull requests, emails or forms were changed or submitted while preparing this pack.
