"""Code shared between the tools app (app/) and the portal app (portal/).

Both are separate processes with separate venvs; anything in here must import
cleanly under both and must not assume either app's Flask context.
"""
