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
      "Beryl",
      "Bexalite",
      "Borase",
      "Const. Materials",
      "Copper",
      "Corundum",
      "Dolivine",
      "Feynmaline",
      "Glacosite",
      "Hephaestanite",
      "Ice",
      "Iron",
      "Lindinium",
      "Quantanium",
      "Quartz",
      "Recy. Material Composite",
      "Riccite",
      "Savrilium",
      "Silicon",
      "Stileron",
      "Taranite",
      "Tin",
      "Titanium",
      "Torite",
      "Tungsten"
    ],
    "maxPct": 52.4,
    "source": "solprovision-mining-tool.xlsx :: LEDGER (HEAT_MAP pivot)"
  },
  "locations": [
    {
      "system": "nyx",
      "label": "GLACIEM BELT - BELT",
      "anchor": {
        "kind": "belt",
        "name": "Glaciem Ring"
      },
      "samples": 12,
      "ores": {
        "Ice": 33.3,
        "Torite": 25.0,
        "Iron": 8.3,
        "Lindinium": 25.0,
        "Recy. Material Composite": 8.3
      }
    },
    {
      "system": "nyx",
      "label": "GLACIEM BELT - LEVSKI",
      "anchor": {
        "kind": "body",
        "name": "Delamar"
      },
      "samples": 74,
      "ores": {
        "Lindinium": 21.6,
        "Torite": 21.6,
        "Recy. Material Composite": 9.5,
        "Bexalite": 20.3,
        "Iron": 17.6,
        "Ice": 2.7,
        "Savrilium": 6.8
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - BELT",
      "anchor": {
        "kind": "belt",
        "name": "Keeger Belt"
      },
      "samples": 3,
      "ores": {
        "Savrilium": 33.3,
        "Ice": 33.3,
        "Lindinium": 33.3
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - PSS ALPHA",
      "anchor": {
        "kind": "helio",
        "x": -45804303.1,
        "y": -14351319.0
      },
      "samples": 25,
      "ores": {
        "Bexalite": 32.0,
        "Torite": 20.0,
        "Aluminum": 16.0,
        "Lindinium": 16.0,
        "Ice": 8.0,
        "Savrilium": 4.0,
        "Recy. Material Composite": 4.0
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - PSS DELTA",
      "anchor": {
        "kind": "helio",
        "x": -6432457.1,
        "y": -47567013.3
      },
      "samples": 11,
      "ores": {
        "Ice": 36.4,
        "Aluminum": 9.1,
        "Torite": 36.4,
        "Savrilium": 9.1,
        "Bexalite": 9.1
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - PSS Theta",
      "anchor": {
        "kind": "helio",
        "x": 40472701.1,
        "y": -25806231.9
      },
      "samples": 57,
      "ores": {
        "Torite": 24.6,
        "Lindinium": 21.1,
        "Aluminum": 10.5,
        "Ice": 8.8,
        "Bexalite": 26.3,
        "Savrilium": 8.8
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - QV BRK-204",
      "anchor": {
        "kind": "helio",
        "x": -45105001.8,
        "y": 16416858.0
      },
      "samples": 7,
      "ores": {
        "Lindinium": 14.3,
        "Torite": 42.9,
        "Savrilium": 14.3,
        "Aluminum": 14.3,
        "Bexalite": 14.3
      }
    },
    {
      "system": "nyx",
      "label": "KEEGER BELT - QV BRK-320",
      "anchor": {
        "kind": "helio",
        "x": -27531183.2,
        "y": 39318601.2
      },
      "samples": 45,
      "ores": {
        "Torite": 22.2,
        "Aluminum": 15.6,
        "Recy. Material Composite": 28.9,
        "Bexalite": 13.3,
        "Lindinium": 6.7,
        "Const. Materials": 4.4,
        "Ice": 4.4,
        "Savrilium": 4.4
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - CLUSTER NBD-102",
      "anchor": {
        "kind": "helio",
        "x": 16481986.1,
        "y": -12482403.2
      },
      "samples": 8,
      "ores": {
        "Corundum": 12.5,
        "Torite": 12.5,
        "Aluminum": 37.5,
        "Riccite": 37.5
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RAB-ALPHA",
      "anchor": {
        "kind": "helio",
        "x": 7042819.0,
        "y": 2993307.5
      },
      "samples": 6,
      "ores": {
        "Aluminum": 33.3,
        "Torite": 33.3,
        "Riccite": 33.3
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RAB-KILO",
      "anchor": {
        "kind": "helio",
        "x": 14907563.8,
        "y": -9617700.1
      },
      "samples": 9,
      "ores": {
        "Torite": 11.1,
        "Tin": 22.2,
        "Aluminum": 33.3,
        "Corundum": 11.1,
        "Quartz": 11.1,
        "Riccite": 11.1
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RAB-TUNG",
      "anchor": {
        "kind": "helio",
        "x": 8380397.4,
        "y": 3880161.2
      },
      "samples": 12,
      "ores": {
        "Stileron": 16.7,
        "Tin": 25.0,
        "Corundum": 41.7,
        "Riccite": 8.3,
        "Aluminum": 8.3
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-EVEN",
      "anchor": {
        "kind": "helio",
        "x": 6869973.3,
        "y": -10689136.5
      },
      "samples": 4,
      "ores": {
        "Tin": 50.0,
        "Aluminum": 25.0,
        "Stileron": 25.0
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-LAZO",
      "anchor": {
        "kind": "helio",
        "x": 7439603.7,
        "y": -10249427.2
      },
      "samples": 16,
      "ores": {
        "Torite": 12.5,
        "Riccite": 43.8,
        "Quartz": 25.0,
        "Tin": 18.8
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-NAIN",
      "anchor": {
        "kind": "helio",
        "x": 7883125.1,
        "y": -8919159.3
      },
      "samples": 13,
      "ores": {
        "Aluminum": 15.4,
        "Torite": 30.8,
        "Riccite": 15.4,
        "Tin": 15.4,
        "Quartz": 7.7,
        "Corundum": 7.7,
        "Stileron": 7.7
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-NIGH",
      "anchor": {
        "kind": "helio",
        "x": 5792164.1,
        "y": -6347703.0
      },
      "samples": 21,
      "ores": {
        "Torite": 52.4,
        "Riccite": 19.0,
        "Tin": 9.5,
        "Aluminum": 9.5,
        "Quartz": 9.5
      }
    },
    {
      "system": "pyro",
      "label": "MINING BASE - RMB-ZARF",
      "anchor": {
        "kind": "helio",
        "x": 5030736.2,
        "y": -6916005.1
      },
      "samples": 28,
      "ores": {
        "Torite": 32.1,
        "Riccite": 28.6,
        "Aluminum": 14.3,
        "Tin": 17.9,
        "Quartz": 7.1
      }
    },
    {
      "system": "pyro",
      "label": "PYRO 5 - Fuego",
      "anchor": {
        "kind": "body",
        "name": "Fuego"
      },
      "samples": 11,
      "ores": {
        "Borase": 9.1,
        "Bexalite": 9.1,
        "Feynmaline": 27.3,
        "Iron": 36.4,
        "Aslarite": 9.1,
        "Hephaestanite": 9.1
      }
    },
    {
      "system": "pyro",
      "label": "PYRO 5 - VUUR",
      "anchor": {
        "kind": "body",
        "name": "Vuur"
      },
      "samples": 14,
      "ores": {
        "Agricium": 28.6,
        "Bexalite": 35.7,
        "Aslarite": 21.4,
        "Iron": 7.1,
        "Hephaestanite": 7.1
      }
    },
    {
      "system": "stanton",
      "label": "AARON HALO - BELT",
      "anchor": {
        "kind": "belt",
        "name": "Aaron Halo"
      },
      "samples": 27,
      "ores": {
        "Beryl": 18.5,
        "Ice": 11.1,
        "Iron": 7.4,
        "Recy. Material Composite": 3.7,
        "Silicon": 11.1,
        "Titanium": 11.1,
        "Copper": 11.1,
        "Quantanium": 7.4,
        "Aslarite": 14.8,
        "Aluminum": 3.7
      }
    },
    {
      "system": "stanton",
      "label": "ARCCORP - Arc-L1",
      "anchor": {
        "kind": "lagrange",
        "code": "ARC-L1"
      },
      "samples": 14,
      "ores": {
        "Taranite": 14.3,
        "Tungsten": 28.6,
        "Hephaestanite": 21.4,
        "Corundum": 14.3,
        "Recy. Material Composite": 14.3,
        "Aluminum": 7.1
      }
    },
    {
      "system": "stanton",
      "label": "HURSTON - Magda",
      "anchor": {
        "kind": "body",
        "name": "Magda"
      },
      "samples": 13,
      "ores": {
        "Titanium": 23.1,
        "Iron": 15.4,
        "Aluminum": 7.7,
        "Quantanium": 7.7,
        "Aphorite": 23.1,
        "Glacosite": 7.7,
        "Dolivine": 7.7,
        "Aslarite": 7.7
      }
    },
    {
      "system": "stanton",
      "label": "MICROTECH - MIC-L4",
      "anchor": {
        "kind": "lagrange",
        "code": "MIC-L4"
      },
      "samples": 8,
      "ores": {
        "Agricium": 50.0,
        "Ice": 25.0,
        "Recy. Material Composite": 25.0
      }
    },
    {
      "system": "stanton",
      "label": "MICROTECH - MINING BASE #R7J-WJ7",
      "anchor": {
        "kind": "helio",
        "x": 22475091.3,
        "y": 37162895.7
      },
      "samples": 32,
      "ores": {
        "Iron": 18.8,
        "Copper": 9.4,
        "Recy. Material Composite": 6.2,
        "Beryl": 31.2,
        "Quantanium": 3.1,
        "Ice": 9.4,
        "Silicon": 9.4,
        "Aluminum": 6.2,
        "Titanium": 3.1,
        "Aslarite": 3.1
      }
    }
  ]
};
