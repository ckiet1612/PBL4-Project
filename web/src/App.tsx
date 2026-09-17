const inactiveCapabilities = ["API", "Scheduling", "Workload execution"] as const;

export default function App() {
  return (
    <main className="workspace">
      <section className="status" aria-labelledby="workspace-title">
        <p className="eyebrow">Nexa</p>
        <h1 id="workspace-title">Bootstrap workspace</h1>
        <p className="summary">
          The web toolchain is ready for later product tasks. Runtime services are not active.
        </p>
        <dl className="capabilities">
          {inactiveCapabilities.map((capability) => (
            <div key={capability}>
              <dt>{capability}</dt>
              <dd>Not implemented</dd>
            </div>
          ))}
        </dl>
      </section>
    </main>
  );
}
