import { Button, ButtonProps, SxProps, Theme } from '@mui/material'
import { forwardRef } from 'react'
import { ACTION_BUTTON_VARIANTS } from '../../theme/variants'

export type ActionButtonTone = 'primary' | 'secondary' | 'ghost' | 'danger'

// 允许透传 MUI color（运行时本就透传给 Button 并生效）；新代码优先使用 tone 体系
interface ActionButtonProps extends ButtonProps {
  tone?: ActionButtonTone
  /** 供 component={RouterLink} 场景透传的目标路径 */
  to?: string
}

const DEFAULT_VARIANT: Record<ActionButtonTone, ButtonProps['variant']> = {
  primary: 'contained',
  secondary: 'outlined',
  ghost: 'text',
  danger: 'outlined',
}

const ActionButton = forwardRef<HTMLButtonElement, ActionButtonProps>(function ActionButton(
  { tone = 'secondary', variant, sx, ...props },
  ref,
) {
  const mergedSx = (
    sx
      ? [ACTION_BUTTON_VARIANTS[tone].styles, sx]
      : ACTION_BUTTON_VARIANTS[tone].styles
  ) as SxProps<Theme>

  return (
    <Button
      {...props}
      ref={ref}
      variant={variant ?? DEFAULT_VARIANT[tone]}
      sx={mergedSx}
    />
  )
})

export default ActionButton
