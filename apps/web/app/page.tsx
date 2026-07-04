const services = [
  { name: "Web", status: "ready" },
  { name: "API", status: "ready" },
  { name: "PostgreSQL", status: "ready" },
  { name: "Redis", status: "ready" },
  { name: "MinIO", status: "ready" },
];

export default function Home() {
  return (
    <main>
      <section className="panel">
        <p className="eyebrow">Brand Agent Studio</p>
        <h1>开发环境已就绪</h1>
        <p className="summary">
          当前页面用于验证团队基线。业务页面将从各自功能分支开始开发。
        </p>
        <ul>
          {services.map((service) => (
            <li key={service.name}>
              <span>{service.name}</span>
              <span className="status">{service.status}</span>
            </li>
          ))}
        </ul>
        <div className="links">
          <a href="/api/docs">打开 API 文档</a>
          <a href="/api/v1/health/ready">查看后端状态</a>
        </div>
      </section>
    </main>
  );
}
