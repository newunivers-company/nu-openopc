import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ModeAdvisorPanel } from './ModeAdvisorPanel'

describe('ModeAdvisorPanel', () => {
  it('requires an explicit operator action before changing mode', async () => {
    const user = userEvent.setup()
    const onApply = vi.fn()
    render(<ModeAdvisorPanel currentMode="task" onApply={onApply} />)

    await user.click(screen.getByText('Assess fit'))
    expect(screen.getByText('Task Mode')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Already selected' })).toBeDisabled()

    await user.clear(screen.getByLabelText('Roles'))
    await user.type(screen.getByLabelText('Roles'), '3')
    await user.clear(screen.getByLabelText('Workstreams'))
    await user.type(screen.getByLabelText('Workstreams'), '3')

    expect(screen.getByText('Company Mode')).toBeInTheDocument()
    expect(onApply).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Use Company Mode' }))
    expect(onApply).toHaveBeenCalledWith('company')
  })

  it('withholds mode application while input evidence is incomplete', async () => {
    const user = userEvent.setup()
    render(<ModeAdvisorPanel currentMode="task" onApply={vi.fn()} />)

    await user.click(screen.getByText('Assess fit'))
    await user.click(screen.getByLabelText('Inputs complete'))

    expect(screen.getByText('Clarify first')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Use .* Mode/ })).toBeNull()
    expect(screen.getByText(/Do not start until/)).toBeInTheDocument()
  })
})
