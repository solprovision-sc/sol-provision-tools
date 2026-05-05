## What does this PR do?
<!-- Brief description of the changes in this PR -->


## Type of change
- [ ] Feature (new functionality)
- [ ] Bug fix
- [ ] Config / CI change
- [ ] Other: ___

## Checklist

### All PRs
- [ ] Tested locally before pushing
- [ ] No hardcoded IPs, API keys, or secrets in any changed files
- [ ] `requirements.txt` updated if new packages were added

### Feature branch → dev PRs
- [ ] Tested on `tools-dev.solprovision.com` after merging

### dev → main PRs (releasing to production)
- [ ] ⚠️ **`index.html` NOT in this diff** — prod has Coming Soon cards, dev has full landing page
- [ ] ⚠️ **`common.js` NOT in this diff** — prod nav shows live pages only, dev nav shows all pages
- [ ] All changes have been verified on `tools-dev.solprovision.com` first
- [ ] Any new nav links in `common.js` are intentionally going live on prod
