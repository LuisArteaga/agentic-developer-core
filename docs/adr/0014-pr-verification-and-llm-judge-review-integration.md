# 0014 - Integration der LLM-Judge PR-Reviews zur Merge-Validierung

* **Status**: Accepted
* **Datum**: 2026-06-27
* **Entscheidungsträger**: Luis Arteaga & Antigravity

## Kontext und Problemstellung
Nachdem die Implementierung der Code-Änderungen die lokale Test- und Verifikationsphase (`Verify-Node`) erfolgreich durchlaufen hat, erstellt der Orchestrator einen Pull Request (PR). Der PR wird anschließend durch ein automatisiertes "LLM-as-a-Judge"-Review-System (`scripts/review.py`) auf Sicherheitsrisiken und Architektur-Konformität geprüft. 

Ein reines Abfragen des GitHub-Merge-Status reicht nicht aus, da eine PR trotz ausstehender oder fehlgeschlagener LLM-Reviews fälschlicherweise gemergt werden könnte. Zudem können über den Lebenszyklus einer PR hinweg mehrere Reviews eingereicht werden. Es muss sichergestellt werden, dass der Orchestrator nur solche Reviews zur Bewertung heranzieht, die sich auf den aktuellsten Commit beziehen (Commit-zeitnahe Validierung), um fälschliche Blocks durch veraltete Review-Entscheidungen zu verhindern.

## Entscheidungsfaktoren (Drivers)
* **Sicherheitsgarantie**: Keine PR mit kritischen Sicherheitsmängeln darf gemergt werden.
* **Architektur-Konformität**: Optionale oder strikte Blockierung bei Abweichungen von den Projektkonventionen (ADRs).
* **Aktualität & Korrektheit**: Vermeidung von False Positives durch Ignorieren veralteter Reviews, die vor dem jüngsten Push erstellt wurden.

## Betrachtete Optionen
* **Option 1: Einfaches Polling des PR-Merge-Status**
  Der Orchestrator wartet lediglich darauf, dass die PR den Status `merged` erreicht (z. B. durch manuelles Freigeben oder automatische Hooks).
* **Option 2: Abfrage der PR-Reviews mit Commit-Zeitstempel-Abgleich**
  Der Orchestrator fragt zyklisch die GitHub-Reviews ab und vergleicht das Einreichungsdatum (`submitted_at`) jedes Reviews mit dem Erstellungsdatum des letzten Commits (`git show -s --format=%cI HEAD`). Nur Reviews, die nach oder zeitgleich mit dem letzten Commit erstellt wurden, fließen in die Merge-Entscheidung ein.

## Entscheidung
Wir haben uns für **Option 2** entschieden.

Der Merge-Knoten (`Merge-Node`) fragt die PR-Reviews aktiv über die GitHub-API ab. Durch den Zeitstempel-Abgleich stellen wir sicher, dass nur das Feedback berücksichtigt wird, das sich auf den aktuellen Code-Stand bezieht. Sobald ein Security-Review den Status `FAIL` oder `NEEDS REVIEW` aufweist, wird der Merge-Prozess abgebrochen und das Recovery-System eingeleitet. Über den Parameter `AGENT_BLOCK_ON_ARCH_FAILURE` kann gesteuert werden, ob auch Architektur-Review-Fehler den Merge blockieren.

### Konsequenzen
* **Positiv**:
  * **Erhöhte Sicherheit**: Sicherheits- und Architektur-Blocks werden zuverlässig erkannt und verhindern unkontrollierte Merges.
  * **Robuste Resume-Fähigkeit**: Wenn Korrekturen gepusht werden, werden veraltete Review-Fehler automatisch ignoriert, da sie vor dem neuen Commit-Zeitstempel liegen.
  * **Konfigurierbarkeit**: Einfache Steuerung des Blockverhaltens für Architektur-Richtlinien.
* **Negativ**:
  * **Erhöhte API-Last**: Kontinuierliches Polling der Reviews erfordert regelmäßige Anfragen an die GitHub-API (kann durch sinnvolle Intervalle von 10s gedrosselt werden).

## Inspiration & Referenzen
* **GitHub Branch Protection Rules ([GitHub Docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches))**: Standardmäßig bieten GitHub Branch Protection Rules die Option, PR-Reviews bei neuen Commits als veraltet zu markieren ("Dismiss stale pull request approvals when new commits are pushed"). Unser Ansatz repliziert dieses etablierte Sicherheits-Pattern direkt innerhalb der Orchestrator-Schleife für automatisierte Agenten-Reviews.
* **Kubernetes Prow/Munch bot ([Prow Architecture](https://github.com/kubernetes/test-infra/tree/master/prow))**: Das Prow-System von Kubernetes verwendet feingranulare Status-Checks und Review-Kommentare zur Merge-Steuerung, wobei die Aktualität des Git-Refs kontinuierlich gegen den Validierungsstatus geprüft wird.
