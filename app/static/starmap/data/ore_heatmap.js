// ═══════════════════════════════════════════════════════════════════
//  ORE CONCENTRATION HEAT-MAP DATA  (auto-generated — do not edit)
//  Source: tools/solprovision-mining-tool.xlsx (LEDGER / HEAT_MAP pivot)
//  Regenerate: python tools/gen_ore_heatmap.py
//
//  Per location: % of that location's recorded finds that were each ore
//  (matches the workbook's COUNTA-of-Found-Ore as %-of-row). `anchor`
//  tells the renderer where to drop the heat disk in the live scene.
// ═══════════════════════════════════════════════════════════════════

export const ORE_HEATMAP = {
  "meta": {
    "ores": [
      "Agricium",
      "Aluminum",
      "Aphorite",
      "Aslarite",
      "Beradom",
      "Beryl",
      "Bexalite",
      "Borase",
      "Copper",
      "Corundum",
      "Dolivine",
      "Feynmaline",
      "Glacosite",
      "Gold",
      "Hephaestanite",
      "Ice",
      "Iron",
      "Janalite",
      "Laranite",
      "Lindinium",
      "Ouratite",
      "Quantanium",
      "Quartz",
      "Recy. Material Composite",
      "Riccite",
      "Savrilium",
      "Silicon",
      "Taranite",
      "Tin",
      "Titanium",
      "Torite",
      "Tungsten"
    ],
    "maxPct": 100.0,
    "source": "solprovision-mining-tool.xlsx :: LEDGER (HEAT_MAP pivot)"
  },
  "locations": [
    {
      "system": "nyx",
      "label": "GLACIEAN BELT - Belt",
      "anchor": {
        "kind": "belt",
        "name": "Glaciem Ring"
      },
      "samples": 11,
      "ores": {
        "Savrilium": 27.3,
        "Iron": 9.1,
        "Ice": 9.1,
        "Bexalite": 27.3,
        "Lindinium": 9.1,
        "Aluminum": 9.1,
        "Torite": 9.1
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - Belt",
      "anchor": {
        "kind": "belt",
        "name": "Keeger Belt"
      },
      "samples": 6,
      "ores": {
        "Lindinium": 16.7,
        "Bexalite": 16.7,
        "Iron": 33.3,
        "Torite": 16.7,
        "Aluminum": 16.7
      }
    },
    {
      "system": "pyro",
      "label": "MINING - RAB-KNAP",
      "anchor": {
        "kind": "none"
      },
      "samples": 2,
      "ores": {
        "Tin": 50.0,
        "Riccite": 50.0
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-NIGH",
      "anchor": {
        "kind": "none"
      },
      "samples": 1,
      "ores": {
        "Tin": 100.0
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - Select Site",
      "anchor": {
        "kind": "none"
      },
      "samples": 1,
      "ores": {
        "Tin": 100.0
      }
    },
    {
      "system": "pyro",
      "label": "PYRO 2 - Select Site",
      "anchor": {
        "kind": "body",
        "name": "Monox"
      },
      "samples": 1,
      "ores": {
        "Glacosite": 100.0
      }
    },
    {
      "system": "pyro",
      "label": "PYRO 4 - PY4",
      "anchor": {
        "kind": "body",
        "name": "Pyro IV"
      },
      "samples": 1,
      "ores": {
        "Borase": 100.0
      }
    },
    {
      "system": "pyro",
      "label": "PYRO 4 - Select Site",
      "anchor": {
        "kind": "body",
        "name": "Pyro IV"
      },
      "samples": 1,
      "ores": {
        "Beradom": 100.0
      }
    },
    {
      "system": "stanton",
      "label": "AARON HALO - Belt",
      "anchor": {
        "kind": "belt",
        "name": "Aaron Halo"
      },
      "samples": 12,
      "ores": {
        "Beryl": 8.3,
        "Aslarite": 33.3,
        "Quantanium": 33.3,
        "Recy. Material Composite": 8.3,
        "Aluminum": 8.3,
        "Iron": 8.3
      }
    },
    {
      "system": "stanton",
      "label": "ARCCORP - Arc-L1",
      "anchor": {
        "kind": "lagrange",
        "code": "ARC-L1"
      },
      "samples": 9,
      "ores": {
        "Titanium": 22.2,
        "Silicon": 11.1,
        "Beryl": 11.1,
        "Copper": 11.1,
        "Taranite": 11.1,
        "Corundum": 11.1,
        "Aluminum": 11.1,
        "Tungsten": 11.1
      }
    },
    {
      "system": "stanton",
      "label": "ARCCORP - Arc-L5",
      "anchor": {
        "kind": "lagrange",
        "code": "ARC-L5"
      },
      "samples": 2,
      "ores": {
        "Aslarite": 50.0,
        "Gold": 50.0
      }
    },
    {
      "system": "stanton",
      "label": "ARCCORP - Wala",
      "anchor": {
        "kind": "body",
        "name": "Wala"
      },
      "samples": 44,
      "ores": {
        "Glacosite": 4.5,
        "Laranite": 9.1,
        "Dolivine": 20.5,
        "Beradom": 9.1,
        "Feynmaline": 2.3,
        "Copper": 6.8,
        "Aphorite": 27.3,
        "Beryl": 9.1,
        "Iron": 4.5,
        "Janalite": 2.3,
        "Quantanium": 2.3,
        "Aluminum": 2.3
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - CRU-L1",
      "anchor": {
        "kind": "lagrange",
        "code": "CRU-L1"
      },
      "samples": 1,
      "ores": {
        "Aluminum": 100.0
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - CRU-L4",
      "anchor": {
        "kind": "lagrange",
        "code": "CRU-L4"
      },
      "samples": 2,
      "ores": {
        "Aslarite": 50.0,
        "Gold": 50.0
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - Cellin",
      "anchor": {
        "kind": "body",
        "name": "Cellin"
      },
      "samples": 1,
      "ores": {
        "Dolivine": 100.0
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - Daymar",
      "anchor": {
        "kind": "body",
        "name": "Daymar"
      },
      "samples": 4,
      "ores": {
        "Silicon": 25.0,
        "Titanium": 25.0,
        "Aluminum": 25.0,
        "Beryl": 25.0
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - Yela",
      "anchor": {
        "kind": "body",
        "name": "Yela"
      },
      "samples": 21,
      "ores": {
        "Dolivine": 9.5,
        "Aphorite": 33.3,
        "Beradom": 9.5,
        "Quantanium": 9.5,
        "Glacosite": 9.5,
        "Silicon": 4.8,
        "Quartz": 9.5,
        "Taranite": 4.8,
        "Agricium": 9.5
      }
    },
    {
      "system": "stanton",
      "label": "CRUSADER - Yela Belt",
      "anchor": {
        "kind": "body",
        "name": "Yela"
      },
      "samples": 31,
      "ores": {
        "Ouratite": 12.9,
        "Copper": 12.9,
        "Titanium": 22.6,
        "Ice": 9.7,
        "Iron": 12.9,
        "Recy. Material Composite": 29.0
      }
    },
    {
      "system": "stanton",
      "label": "HURSTON - Aberdeen",
      "anchor": {
        "kind": "body",
        "name": "Aberdeen"
      },
      "samples": 6,
      "ores": {
        "Titanium": 33.3,
        "Aluminum": 33.3,
        "Ouratite": 16.7,
        "Quantanium": 16.7
      }
    },
    {
      "system": "stanton",
      "label": "HURSTON - HUR-L3",
      "anchor": {
        "kind": "lagrange",
        "code": "HUR-L3"
      },
      "samples": 3,
      "ores": {
        "Iron": 33.3,
        "Bexalite": 33.3,
        "Titanium": 33.3
      }
    },
    {
      "system": "stanton",
      "label": "HURSTON - HUR-L4",
      "anchor": {
        "kind": "lagrange",
        "code": "HUR-L4"
      },
      "samples": 5,
      "ores": {
        "Hephaestanite": 60.0,
        "Laranite": 20.0,
        "Aluminum": 20.0
      }
    },
    {
      "system": "stanton",
      "label": "MICROTECH - Euterpe",
      "anchor": {
        "kind": "body",
        "name": "Euterpe"
      },
      "samples": 1,
      "ores": {
        "Quantanium": 100.0
      }
    },
    {
      "system": "stanton",
      "label": "MICROTECH - MIC-L1",
      "anchor": {
        "kind": "lagrange",
        "code": "MIC-L1"
      },
      "samples": 1,
      "ores": {
        "Aluminum": 100.0
      }
    },
    {
      "system": "stanton",
      "label": "MICROTECH - Mic",
      "anchor": {
        "kind": "body",
        "name": "microTech"
      },
      "samples": 1,
      "ores": {
        "Aslarite": 100.0
      }
    }
  ]
};
