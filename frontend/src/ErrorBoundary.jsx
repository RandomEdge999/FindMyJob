import React from 'react'

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch() {}

  render() {
    if (this.state.hasError) {
      return (
        <div className="page-shell">
          <section className="panel section-panel">
            <div className="section-head">
              <div>
                <div className="eyebrow">Frontend Error</div>
                <h2>Something went wrong</h2>
                <p className="section-copy">Reload the page to recover the operator console.</p>
              </div>
            </div>
          </section>
        </div>
      )
    }
    return this.props.children
  }
}
