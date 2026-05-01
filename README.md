# Sol Provision Tools

Data intelligence platform for the Sol Provision Star Citizen organization.

**Live site:** https://tools.solprovision.com

## Stack
- Flask + Gunicorn (backend)
- SQLite via DataForge pipeline (database)
- Vanilla JS + Jinja2 (frontend)
- Nginx (web server)
- GitHub Actions (CI/CD)

## Database
`dataforge.db` is not version controlled. It is deployed separately
via SCP from the DataForge pipeline on the extraction machine.

## Deployment
Merging to `main` triggers automatic deployment to the VPS via
GitHub Actions. See `.github/workflows/deploy.yml`.
