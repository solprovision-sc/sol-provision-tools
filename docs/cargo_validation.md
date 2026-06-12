# Cargo Validation — game grids vs RSI published

**Patch:** `4.8.1-live.11875683` &nbsp;|&nbsp; **Roster:** 233 &nbsp;|&nbsp; **RSI-matched:** 209 &nbsp;|&nbsp; **Unmatched (no RSI):** 24 &nbsp;|&nbsp; **Significant gaps:** 48

Display logic now: **`rsi_cargo_scu` when present (authoritative published capacity), else the in-game `cargo_scu` grid sum.** This table validates that choice — every row where the two sources disagree materially.

## Significant discrepancies (now corrected to RSI)

| Ship | in-game grid | RSI published | Δ |
|---|---:|---:|---:|
| Aegis Javelin `aegs_javelin` | — | **5,400** | +5,400 |
| MISC Hull C `misc_hull_c` | 576 | **4,608** | +4,032 |
| Drake Ironclad Assault `drak_ironclad_assault` | 2 | **1,440** | +1,438 |
| Aegis Idris-P `aegs_idris_p` | 32 | **1,374** | +1,342 |
| Aegis Idris-M `aegs_idris_m` | 32 | **1,326** | +1,294 |
| Drake Ironclad `drak_ironclad` | 1,096 | **2,204** | +1,108 |
| MISC Hull B `misc_hull_b` | 32 | **512** | +480 |
| Drake Caterpillar `drak_caterpillar` | 204 | **576** | +372 |
| Anvil Carrack `anvl_carrack` | 88 | **456** | +368 |
| RSI Polaris `rsi_polaris` | 288 | **576** | +288 |
| MISC Starfarer Gemini `misc_starfarer_gemini` | 28 | **291** | +263 |
| MISC Starfarer Teach's Special `misc_starfarer_teach` | 28 | **291** | +263 |
| Aegis Reclaimer `aegs_reclaimer` | 165 | **420** | +255 |
| Aegis Reclaimer Teach's Special `aegs_reclaimer_teach` | 180 | **420** | +240 |
| MISC Starlancer MAX `misc_starlancer_max` | 64 | **224** | +160 |
| RSI Hermes `rsi_hermes` | 144 | **288** | +144 |
| RSI Zeus Mk II CL `rsi_zeus_cl` | 6 | **128** | +122 |
| Aegis Retaliator `aegs_retaliator` | 102 | **0** | -102 |
| Argo MOTH `argo_moth` | 320 | **224** | -96 |
| RSI Constellation Phoenix Emerald `rsi_constellation_phoenix_emerald` | 5 | **80** | +75 |
| MISC Starlancer TAC `misc_starlancer_tac` | 28 | **96** | +68 |
| Origin 890 Jump `orig_890jump` | 328 | **388** | +60 |
| MISC Starfarer `misc_starfarer` | 233 | **291** | +58 |
| Crusader M2 Hercules Starlifter `crus_starlifter_m2` | 522 | **468** | -54 |
| MISC Hull A `misc_hull_a` | 16 | **64** | +48 |
| Drake Cutlass Blue `drak_cutlass_blue` | 46 | **12** | -34 |
| Crusader C1 Spirit `crus_spirit_c1` | 32 | **64** | +32 |
| Drake Golem `drak_golem` | 0 | **32** | +32 |
| Drake Golem Teach's Special `drak_golem_teach` | 0 | **32** | +32 |
| Aegis Tiburon `aegs_tiburon` | 10 | **40** | +30 |
| Origin 600i `orig_600i` | 40 | **16** | -24 |
| RSI Apollo Medivac `rsi_apollo_medivac` | 8 | **32** | +24 |
| RSI Apollo Triage `rsi_apollo_triage` | 8 | **32** | +24 |
| MISC Reliant Kore `misc_reliant` | 28 | **6** | -22 |
| Esperia Prowler Utility `espr_prowler_utility` | 16 | **32** | +16 |
| MISC Fortune Teach's Special `misc_fortune_teach` | 28 | **12** | -16 |
| Drake Vulture Teach's Special `drak_vulture_teach` | 1 | **12** | +11 |
| Gatac Syulen `gama_syulen` | 1 | **6** | +5 |
| Syulen PYAM Exec `gama_syulen_exec_military` | 1 | **6** | +5 |
| Crusader Intrepid `crus_intrepid` | 4 | **8** | +4 |
| RSI Aurora Mk I MR `rsi_aurora_gs_mr` | 6 | **2** | -4 |
| RSI Aurora Mk I ES `rsi_aurora_gs_es` | 6 | **3** | -3 |
| RSI Aurora Mk I LN `rsi_aurora_gs_ln` | 6 | **3** | -3 |
| RSI Aurora Mk I  LX `rsi_aurora_gs_lx` | 6 | **3** | -3 |
| Drake Cutter Rambler `drak_cutter_rambler` | 4 | **2** | -2 |
| Drake Cutter Scout `drak_cutter_scout` | 4 | **2** | -2 |
| Anvil F7C Hornet Mk I `anvl_hornet_f7c` | 1 | **2** | +1 |
| RSI Aurora Mk II `rsi_aurora_mk2` | 1 | **2** | +1 |

## Unmatched — no RSI counterpart (still using in-game grid)

These the RSI matcher couldn't pair (edition/variant naming, or genuinely absent from the ship matrix). They keep the game-file `cargo_scu`.

| Ship | in-game cargo |
|---|---:|
| Anvil F8A Lightning `anvl_lightning_f8` | 1 |
| Anvil F8C Lightning `anvl_lightning_f8c` | 1 |
| Aopoa Khartu-al `xian_scout` | 1 |
| Aopoa Nox `xian_nox` | 0 |
| Aopoa Nox Kue `xian_nox_kue` | 0 |
| C.O. HoverQuad `cnou_hoverquad` | 1 |
| C.O. Nomad `cnou_nomad` | 24 |
| C.O. Nomad Teach's Special `cnou_nomad_teach` | 24 |
| Drake Command Module `drak_command_module` | — |
| Drake Dragonfly Star Kitten `drak_dragonfly_pink` | 0 |
| Esperia Blade `vncl_blade` | 1 |
| Esperia Glaive `vncl_glaive` | 1 |
| Esperia Stinger `vncl_stinger` | 1 |
| F8C Lightning PYAM Exec `anvl_lightning_f8c_exec_military` | 1 |
| Grey's Shiv `glsn_shiv` | 32 |
| Greycat MDC `grin_mdc` | — |
| Kruger L-21 Wolf `krig_l21_wolf` | 1 |
| Kruger L-22 Alpha Wolf `krig_l22_alphawolf` | 1 |
| MISC Starlite `misc_starlite` | 28 |
| RSI Lynx `rsi_lynx` | — |
| RSI Meteor `rsi_meteor` | — |
| RSI Meteor PYAM Exec `rsi_meteor_collector_military` | — |
| rsi_ursa_medivac_stealth `rsi_ursa_medivac_stealth` | — |
| rsi_ursa_rover_prison `rsi_ursa_rover_prison` | — |

## In agreement (161)

Ships where in-game grid and RSI match (within tolerance) — no change in displayed value. Listed for completeness.

`aegs_avenger_stalker`, `aegs_avenger_titan`, `aegs_avenger_titan_renegade`, `aegs_avenger_warlock`, `aegs_eclipse`, `aegs_gladius`, `aegs_gladius_dunlevy`, `aegs_gladius_pir`, `aegs_gladius_valiant`, `aegs_hammerhead`, `aegs_hammerhead_gs`, `aegs_redeemer`, `aegs_sabre`, `aegs_sabre_comet`, `aegs_sabre_firebird`, `aegs_sabre_peregrine`, `aegs_sabre_raven`, `aegs_vanguard`, `aegs_vanguard_harbinger`, `aegs_vanguard_hoplite`, `aegs_vanguard_sentinel`, `anvl_arrow`, `anvl_asgard`, `anvl_ballista`, `anvl_ballista_dunestalker`, `anvl_ballista_snowblind`, `anvl_c8_pisces`, `anvl_c8r_pisces`, `anvl_c8x_pisces_expedition`, `anvl_carrack_expedition`, `anvl_centurion`, `anvl_gladiator`, `anvl_hawk`, `anvl_hornet_f7a_mk1`, `anvl_hornet_f7a_mk2`, `anvl_hornet_f7a_mk2_exec_military`, `anvl_hornet_f7c_mk2`, `anvl_hornet_f7c_wildfire`, `anvl_hornet_f7cm`, `anvl_hornet_f7cm_heartseeker`, `anvl_hornet_f7cm_mk2`, `anvl_hornet_f7cm_mk2_heartseeker`, `anvl_hornet_f7cr`, `anvl_hornet_f7cr_mk2`, `anvl_hornet_f7cs`, `anvl_hornet_f7cs_mk2`, `anvl_hurricane`, `anvl_paladin`, `anvl_spartan`, `anvl_terrapin`, `anvl_terrapin_medic`, `anvl_valkyrie`, `argo_csv_cargo`, `argo_mole`, `argo_mole_teach`, `argo_mpuv`, `argo_mpuv_1t`, `argo_mpuv_transport`, `argo_raft`, `argo_srv`, `banu_defender`, `cnou_mustang_alpha`, `cnou_mustang_beta`, `cnou_mustang_delta`, `cnou_mustang_gamma`, `cnou_mustang_omega`, `crus_spirit_a1`, `crus_star_runner`, `crus_starfighter_inferno`, `crus_starfighter_ion`, `crus_starlifter_a2`, `crus_starlifter_c2`, `drak_buccaneer`, `drak_caterpillar_pirate`, `drak_clipper`, `drak_corsair`, `drak_corsair_exec_military`, `drak_cutlass_black`, `drak_cutlass_black_exec_military`, `drak_cutlass_red`, `drak_cutlass_steel`, `drak_cutter`, `drak_dragonfly`, `drak_dragonfly_yellow`, `drak_golem_ox`, `drak_herald`, `drak_mule`, `drak_pitbull`, `drak_vulture`, `espr_prowler`, `espr_talon`, `espr_talon_shrike`, `grin_mtc`, `grin_ptv`, `grin_roc`, `grin_roc_ds`, `grin_stv`, `grin_utv`, `krig_p52_merlin`, `krig_p72_archimedes`, `krig_p72_archimedes_emerald`, `misc_fortune`, `misc_freelancer`, `misc_freelancer_dur`, `misc_freelancer_max`, `misc_freelancer_mis`, `misc_fury`, `misc_fury_lx`, `misc_fury_miru`, `misc_prospector`, `misc_razor`, `misc_razor_ex`, `misc_razor_lx`, `misc_reliant_mako`, `misc_reliant_sen`, `misc_reliant_tana`, `mrai_guardian`, `mrai_guardian_mx`, `mrai_guardian_qi`, `orig_100i`, `orig_125a`, `orig_135c`, `orig_300i`, `orig_315p`, `orig_325a`, `orig_350r`, `orig_400i`, `orig_600i_touring`, `orig_85x`, `orig_m50`, `orig_m80`, `orig_x1`, `orig_x1_force`, `orig_x1_velocity`, `rsi_aurora_gs_cl`, `rsi_aurora_gs_se`, `rsi_constellation_andromeda`, `rsi_constellation_aquila`, `rsi_constellation_phoenix`, `rsi_constellation_taurus`, `rsi_mantis`, `rsi_perseus`, `rsi_salvation`, `rsi_scorpius`, `rsi_scorpius_antares`, `rsi_ursa_medivac`, `rsi_ursa_rover`, `rsi_ursa_rover_emerald`, `rsi_zeus_es`, `rsi_zeus_es_collector_indust`, `tmbl_cyclone`, `tmbl_cyclone_aa`, `tmbl_cyclone_mt`, `tmbl_cyclone_rc`, `tmbl_cyclone_rn`, `tmbl_cyclone_tr`, `tmbl_nova`, `tmbl_storm`, `tmbl_storm_aa`, `vncl_scythe`, `xnaa_santokyai`
