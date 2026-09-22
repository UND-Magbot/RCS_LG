import "./under-development.css";

export interface UnderDevelopmentProps {
  pageName: string;
}

export function UnderDevelopment({ pageName }: UnderDevelopmentProps) {
  return (
    <div className="under-dev">
      <section className="under-dev__map">
        <div className="under-dev__content">
          <h2 className="under-dev__title">{pageName}</h2>
          <p className="under-dev__message">
            This page is currently under development.
          </p>
        </div>
      </section>
    </div>
  );
}
