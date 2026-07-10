# Sovereign Demographic Engine — Results

_Last frame: tick **326**, sim **32.6s**, backend **local**_

## Headline

- **6/6** agents **SETTLED** at El Paso
- **Origin (Zacatecas) remittance wealth:** **$6,497.25**
- **Capital pulses:** 24 settled / 24 spawned
- **Total remitted by agents:** **$6,497.26**

## Agents

| Name | State | Wealth left | Sent home | Yield | Pulses |
|------|-------|------------:|----------:|------:|-------:|
| Jose_Rojas | SETTLED | $602 | $898 | 59.9% | 4 |
| Joseph_Rojas | SETTLED | $581 | $869 | 59.9% | 4 |
| Maria_Garcia | SETTLED | $881 | $1,319 | 60.0% | 4 |
| Jose_Rojas | SETTLED | $640 | $960 | 60.0% | 4 |
| Ana_Lopez | SETTLED | $391 | $589 | 60.1% | 4 |
| Carlos_Mendez | SETTLED | $1,237 | $1,863 | 60.1% | 4 |

## Pattern learning

| Field | Value |
|-------|------:|
| episodes | 96 |
| remit_fraction | 0.21000000000000005 |
| risk_bias | -0.060000000000000046 |
| α (jobs) | 1.7200000000000006 |
| β (network) | 1.7760000000000031 |
| γ (friction) | 1.6160000000000023 |
| migrate_ok | 0.953930201013048 |

## Story

6 people migrated Zacatecas → El Paso and sent **$6,497** home as remittances. Origin aggregate wealth is **$6,497**.

## How to re-run

```bash
cd ~/projects/sovereign-demographic-engine
source .venv/bin/activate
python layer4_spatial/spatial_engine.py --proto --no-api --quiet
python scripts/show_results.py
```
