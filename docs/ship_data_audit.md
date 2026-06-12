# Ship Data Self-Consistency Audit

**Patch:** `4.8.1-live.11875683` &nbsp;|&nbsp; **Ships audited:** 233 (204 ships, 29 ground vehicles) &nbsp;|&nbsp; **Clean:** 57 &nbsp;|&nbsp; **Flagged:** 176

> Scope: our extracted data only (the values the ship-detail sidebar renders). This validates internal consistency — it does **not** compare against SPViewer (their data sits behind an authenticated API and a newer 4.8.1 build, `11952654` vs our `11875683`).

## Summary — issues by frequency

| Severity | Code | Ships affected | Scope | Meaning |
|---|---|---:|---|---|
| ERR | `mass_missing` | 111 (48%) | **pattern** | ship mass_kg null/0 |
| ERR | `power_missing` | 3 (1%) | ship-specific | no power plant in loadout |
| WARN | `qd_missing` | 23 (10%) | ship-specific | no quantum drive |
| WARN | `cooler_missing` | 4 (2%) | ship-specific | no cooler |
| WARN | `pilot_dps_zero` | 4 (2%) | ship-specific | pilot guns present but DPS+alpha = 0 (ammo gap) |
| INFO | `armor_cs_null` | 94 (40%) | **pattern** | no armor cross-section multiplier |
| INFO | `shield_missing` | 12 (5%) | ship-specific | no shield |

## Broad patterns (≥25% of fleet)

These are single extractor/server gaps, not per-ship bugs — fix once, fixes many. Affected ships are listed compactly here and **not** repeated in the per-ship section.

### `mass_missing` — 111 ships

Base hulls carry `mass_kg`, but **variants don't inherit it** (e.g. `anvl_ballista`=33,276 kg → `anvl_ballista_dunestalker`=null); a few base ships are also null (Avenger Stalker). The sidebar Mass row shows blank for all of these. Fix = propagate mass from base→variant in the vehicle extractor.

<details><summary>affected ships</summary>

`aegs_avenger_stalker`, `aegs_avenger_titan`, `aegs_avenger_titan_renegade`, `aegs_avenger_warlock`, `aegs_gladius_dunlevy`, `aegs_gladius_pir`, `aegs_gladius_valiant`, `aegs_idris_m`, `aegs_reclaimer_teach`, `aegs_sabre_comet`, `aegs_vanguard_harbinger`, `aegs_vanguard_hoplite`, `aegs_vanguard_sentinel`, `anvl_ballista_dunestalker`, `anvl_ballista_snowblind`, `anvl_c8_pisces`, `anvl_c8r_pisces`, `anvl_c8x_pisces_expedition`, `anvl_carrack_expedition`, `anvl_hornet_f7a_mk2`, `anvl_hornet_f7c`, `anvl_hornet_f7c_mk2`, `anvl_hornet_f7c_wildfire`, `anvl_hornet_f7cm`, `anvl_hornet_f7cm_heartseeker`, `anvl_hornet_f7cm_mk2_heartseeker`, `anvl_hornet_f7cr_mk2`, `anvl_hornet_f7cs`, `anvl_hornet_f7cs_mk2`, `anvl_lightning_f8c`, `anvl_lightning_f8c_exec_military`, `anvl_terrapin_medic`, `argo_mole_teach`, `argo_mpuv_transport`, `cnou_mustang_alpha`, `cnou_mustang_beta`, `cnou_mustang_delta`, `cnou_mustang_gamma`, `cnou_mustang_omega`, `cnou_nomad_teach`, `crus_spirit_c1`, `crus_starfighter_inferno`, `crus_starfighter_ion`, `crus_starlifter_a2`, `crus_starlifter_c2`, `crus_starlifter_m2`, `drak_caterpillar_pirate`, `drak_corsair_exec_military`, `drak_cutlass_black_exec_military`, `drak_cutlass_steel`, `drak_cutter_rambler`, `drak_cutter_scout`, `drak_dragonfly_pink`, `drak_dragonfly_yellow`, `drak_golem_teach`, `drak_ironclad_assault`, `drak_vulture_teach`, `espr_talon_shrike`, `gama_syulen_exec_military`, `grin_mdc`, `grin_mtc`, `krig_l22_alphawolf`, `krig_p72_archimedes`, `krig_p72_archimedes_emerald`, `misc_fortune_teach`, `misc_freelancer_dur`, `misc_freelancer_max`, `misc_freelancer_mis`, `misc_fury_miru`, `misc_razor_ex`, `misc_razor_lx`, `misc_reliant_mako`, `misc_reliant_sen`, `misc_reliant_tana`, `misc_starfarer_gemini`, `misc_starfarer_teach`, `misc_starlancer_max`, `mrai_guardian_qi`, `orig_125a`, `orig_135c`, `orig_315p`, `orig_325a`, `orig_350r`, `orig_600i_touring`, `orig_x1_force`, `orig_x1_velocity`, `rsi_aurora_gs_es`, `rsi_aurora_gs_ln`, `rsi_aurora_gs_lx`, `rsi_aurora_gs_mr`, `rsi_aurora_gs_se`, `rsi_constellation_andromeda`, `rsi_constellation_aquila`, `rsi_constellation_phoenix`, `rsi_constellation_phoenix_emerald`, `rsi_constellation_taurus`, `rsi_hermes`, `rsi_meteor_collector_military`, `rsi_scorpius_antares`, `rsi_ursa_medivac`, `rsi_ursa_medivac_stealth`, `rsi_ursa_rover_emerald`, `rsi_ursa_rover_prison`, `tmbl_cyclone_aa`, `tmbl_cyclone_mt`, `tmbl_cyclone_rc`, `tmbl_cyclone_rn`, `tmbl_cyclone_tr`, `tmbl_storm_aa`, `vncl_glaive`, `xian_nox_kue`

</details>

### `armor_cs_null` — 94 ships

No armor cross-section multiplier extracted for these hulls, so the sidebar CS (front·side·top) shows blank and the signature model can't apply an armor CS scale. Fix = extract `signal_cross_section` for all ship armor, or fall back to 1.0.

<details><summary>affected ships</summary>

`aegs_eclipse`, `aegs_reclaimer`, `aegs_reclaimer_teach`, `aegs_vanguard`, `aegs_vanguard_harbinger`, `aegs_vanguard_hoplite`, `aegs_vanguard_sentinel`, `anvl_arrow`, `anvl_asgard`, `anvl_carrack`, `anvl_carrack_expedition`, `anvl_hawk`, `anvl_hurricane`, `anvl_lightning_f8`, `anvl_lightning_f8c`, `anvl_lightning_f8c_exec_military`, `anvl_paladin`, `anvl_terrapin`, `anvl_terrapin_medic`, `anvl_valkyrie`, `banu_defender`, `cnou_hoverquad`, `cnou_nomad`, `cnou_nomad_teach`, `crus_intrepid`, `crus_spirit_a1`, `crus_spirit_c1`, `crus_star_runner`, `crus_starfighter_inferno`, `crus_starfighter_ion`, `crus_starlifter_a2`, `crus_starlifter_c2`, `crus_starlifter_m2`, `drak_buccaneer`, `drak_caterpillar`, `drak_caterpillar_pirate`, `drak_clipper`, `drak_command_module`, `drak_corsair`, `drak_corsair_exec_military`, `drak_cutlass_steel`, `drak_cutter`, `drak_cutter_rambler`, `drak_cutter_scout`, `drak_dragonfly`, `drak_dragonfly_pink`, `drak_dragonfly_yellow`, `drak_golem`, `drak_golem_ox`, `drak_golem_teach`, `drak_herald`, `drak_ironclad`, `drak_ironclad_assault`, `drak_pitbull`, `drak_vulture`, `drak_vulture_teach`, `espr_prowler`, `espr_prowler_utility`, `espr_talon`, `espr_talon_shrike`, `gama_syulen`, `gama_syulen_exec_military`, `krig_l21_wolf`, `krig_l22_alphawolf`, `misc_fortune`, `misc_fortune_teach`, `misc_fury`, `misc_fury_lx`, `misc_fury_miru`, `misc_hull_a`, `misc_hull_b`, `misc_hull_c`, `misc_prospector`, `misc_razor`, `misc_razor_ex`, `misc_razor_lx`, `misc_starlancer_max`, `misc_starlancer_tac`, `misc_starlite`, `mrai_guardian`, `mrai_guardian_mx`, `mrai_guardian_qi`, `orig_400i`, `orig_85x`, `orig_m80`, `rsi_apollo_medivac`, `rsi_apollo_triage`, `rsi_hermes`, `rsi_mantis`, `rsi_meteor`, `rsi_meteor_collector_military`, `rsi_salvation`, `xian_nox`, `xian_nox_kue`

</details>

## Ship-specific anomalies

Ships with flags **outside** the broad patterns above — the actionable specifics. Worst severity first.

### Argo MPUV Personnel &nbsp;`argo_mpuv_transport`
- **WARN** `qd_missing` — no quantum drive

### Drake Dragonfly Star Kitten &nbsp;`drak_dragonfly_pink`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Drake Dragonfly Yellowjacket &nbsp;`drak_dragonfly_yellow`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Kruger P-72 Archimedes &nbsp;`krig_p72_archimedes`
- **WARN** `qd_missing` — no quantum drive

### Kruger P-72 Archimedes Emerald &nbsp;`krig_p72_archimedes_emerald`
- **WARN** `qd_missing` — no quantum drive

### Mirai Fury MX &nbsp;`misc_fury_miru`
- **WARN** `qd_missing` — no quantum drive

### Origin 315p &nbsp;`orig_315p`
- **ERR** `power_missing` — no power plant
- **WARN** `qd_missing` — no quantum drive
- **WARN** `cooler_missing` — no cooler
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Origin 325a &nbsp;`orig_325a`
- **ERR** `power_missing` — no power plant
- **WARN** `qd_missing` — no quantum drive
- **WARN** `cooler_missing` — no cooler
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Origin 350r &nbsp;`orig_350r`
- **ERR** `power_missing` — no power plant
- **WARN** `qd_missing` — no quantum drive
- **WARN** `cooler_missing` — no cooler
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Origin X1 Force &nbsp;`orig_x1_force`
- **WARN** `qd_missing` — no quantum drive

### Origin X1 Velocity &nbsp;`orig_x1_velocity`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Aopoa Nox Kue &nbsp;`xian_nox_kue`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Aegis Javelin &nbsp;`aegs_javelin`
- **WARN** `qd_missing` — no quantum drive
- **WARN** `cooler_missing` — no cooler
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Aegis Tiburon &nbsp;`aegs_tiburon`
- **WARN** `pilot_dps_zero` — 1 pilot weapon group(s) but pilot DPS+alpha = 0 (ammo gap?)

### Argo MPUV Cargo &nbsp;`argo_mpuv`
- **WARN** `qd_missing` — no quantum drive

### Argo MPUV Tractor &nbsp;`argo_mpuv_1t`
- **WARN** `qd_missing` — no quantum drive
- **WARN** `pilot_dps_zero` — 1 pilot weapon group(s) but pilot DPS+alpha = 0 (ammo gap?)

### Argo RAFT &nbsp;`argo_raft`
- **WARN** `pilot_dps_zero` — 2 pilot weapon group(s) but pilot DPS+alpha = 0 (ammo gap?)

### Argo SRV &nbsp;`argo_srv`
- **WARN** `pilot_dps_zero` — 1 pilot weapon group(s) but pilot DPS+alpha = 0 (ammo gap?)

### C.O. HoverQuad &nbsp;`cnou_hoverquad`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Drake Dragonfly &nbsp;`drak_dragonfly`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Drake Pitbull &nbsp;`drak_pitbull`
- **WARN** `qd_missing` — no quantum drive

### Kruger P-52 Merlin &nbsp;`krig_p52_merlin`
- **WARN** `qd_missing` — no quantum drive

### Mirai Fury &nbsp;`misc_fury`
- **WARN** `qd_missing` — no quantum drive

### Mirai Fury LX &nbsp;`misc_fury_lx`
- **WARN** `qd_missing` — no quantum drive

### Origin X1 &nbsp;`orig_x1`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)

### Aopoa Nox &nbsp;`xian_nox`
- **WARN** `qd_missing` — no quantum drive
- **INFO** `shield_missing` — no shield (may be correct for light craft)
